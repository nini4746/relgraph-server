# relgraph-server

아이템 동시 발생을 누적하는 인메모리 관계 그래프 기반 추천·검색 서버.

## 설계 요약

- **그래프**: 무방향 가중 엣지(`{(a,b): weight}`), 노드 인접 집합
- **이벤트 가중치**: view=1.0, click=2.0, cart=4.0, purchase=8.0
- **세션 윈도우**: 사용자별 최근 5개 이벤트, 30분 갭이면 윈도우 리셋
- **시간 감쇠**: 윈도우 내 짝의 시간 차에 따라 0.1~1.0 곱
- **추천 전략**:
  - `weight` (기본): 아이템 인접 노드를 엣지 가중치 내림차순
  - `random_walk`: 가중치 비례 샘플링 + restart, 시드 고정 가능 → 재현 가능한 PPR-style
  - `/recommend/user`: 사용자 세션 윈도우 기반 개인화 (이미 본 아이템 제외)
- **검색**: 이름 부분일치 + 태그 일치, 노드 중심성(인접 가중치 합)으로 랭킹. 전체 순회 없이 **역방향 문자 n-gram 색인**(bigram)으로 후보를 좁힌 뒤 후보만 검증 → 랭킹 (아래 "검색 색인" 참고)
- **서브그래프**: `GET /subgraph?item_id=...&depth=2` → 가시화·디버깅용 BFS 추출
- **운영 도구**: `POST /admin/decay`(가중치 감쇠+프루닝), `POST /admin/compact`(스냅샷+WAL 회전)
- ML/외부 그래프 DB 사용하지 않음. 표준 라이브러리 + FastAPI만 사용.
- **영속화**: WAL (`relgraph/wal.py`) + 스냅샷 — 부팅 시 재생, 운영 시 append-only, 컴팩션으로 WAL 잘라냄.
- **관측**: Prometheus exposition (`GET /metrics`) — 이벤트/추천(전략별 라벨)/검색 카운터, 추천 P50/P95 히스토그램.

## 실행

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn relgraph.api:app --reload
```

## API 예시

```bash
curl -X POST localhost:8000/items/bulk -H 'content-type: application/json' \
     -d '[{"id":"p1","name":"Apple","tags":["fruit"]},{"id":"p2","name":"Banana","tags":["fruit"]}]'

curl -X POST localhost:8000/events -H 'content-type: application/json' \
     -d '{"user_id":"u1","item_id":"p1","action":"view","ts":1.0}'
curl -X POST localhost:8000/events -H 'content-type: application/json' \
     -d '{"user_id":"u1","item_id":"p2","action":"purchase","ts":2.0}'

curl -X POST localhost:8000/recommend -H 'content-type: application/json' \
     -d '{"item_id":"p1","k":5}'

curl 'localhost:8000/search?q=fruit'
```

## 테스트

```bash
.venv/bin/pytest -v
```

48개 테스트 통과 (graph + api + wal/metrics + random-walk/subgraph/decay/compact + n-gram 검색 색인).

## 검색 색인 (inverted n-gram index)

- **구조**: `dict[str, set[item_id]]` — bigram(`NGRAM_N=2`) → 그 bigram을 (이름 또는 태그에) 포함하는 아이템 id 집합.
- **정규화**: 소문자화. 아이템의 `name`과 각 `tag`에서 뽑은 bigram의 합집합을 색인.
- **조회**: 질의를 bigram으로 분해 → 각 bigram의 posting list를 (가장 작은 것부터) 교집합 → 후보 집합. 후보에 대해서만 실제 부분일치/태그 일치를 검증한 뒤 중심성으로 랭킹. 참 매치는 질의의 모든 bigram을 포함하므로 후보 집합은 참 매치의 상위집합 → 결과는 기존 선형 스캔과 **완전히 동일**(테스트로 증명).
- **증분 유지**: `upsert`/`bulk_upsert`(갱신 시 옛 n-gram 제거 후 재삽입)/`remove_item`(아이템과 함께 색인 항목 제거, 빈 posting은 삭제)에서 O(글자 수)로 갱신. 아이템 캡과 함께 메모리 상한. 스냅샷 로드·WAL 재생 시 색인 재구축.
- **짧은 질의 엣지**: `NGRAM_N`보다 짧은 질의(1글자)는 n-gram이 없어 `SEARCH_FALLBACK_CAP`(=100k) 개로 상한을 둔 유계 스캔으로 폴백(최악 상수 시간). 빈 질의는 모든 아이템 매치(기존 동작과 동일).
- **성능**(`scripts/bench_search.py`, 50k 아이템, 다양한 어휘): 선택도 높은 질의에서 선형 스캔 대비 약 5~29x, 매치 없는 다중 토큰 질의는 1000x+.

## 성능 (참고치, 단일 프로세스)

`scripts/seed.py` 결과 (10,000 아이템 / 500,000 이벤트):

| 항목 | 값 |
|------|-----|
| ingest 처리량 | ~580k events/sec |
| recommend P50 | 0.003 ms |
| recommend P95 | 0.006 ms |
| recommend P99 | 0.008 ms |

## 한계

- 단일 프로세스 인메모리. 수평 확장은 sharding 필요.
- 아이템/엣지는 캡까지 누적(초과 시 신규 아이템 거부·약한 엣지 축출). 실제 운영에는 LRU/감쇠 일괄 작업 병행 권장.
- 검색은 문자 n-gram(bigram) 부분일치 색인. 형태소 분석은 미적용(문자 단위라 CJK 부분일치는 동작하나 어간/동의어 확장은 없음).

## 추가 도구

```bash
.venv/bin/python scripts/seed.py         # 10k items / 500k events 추천 벤치
.venv/bin/python scripts/bench_search.py # 50k items 검색: 선형 vs n-gram 색인
curl localhost:8000/metrics              # Prometheus exposition
```
