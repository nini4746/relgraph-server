# relgraph-server

아이템 동시 발생을 누적하는 인메모리 관계 그래프 기반 추천·검색 서버.

## 설계 요약

- **그래프**: 무방향 가중 엣지(`{(a,b): weight}`), 노드 인접 집합
- **이벤트 가중치**: view=1.0, click=2.0, cart=4.0, purchase=8.0
- **세션 윈도우**: 사용자별 최근 5개 이벤트, 30분 갭이면 윈도우 리셋
- **시간 감쇠**: 윈도우 내 짝의 시간 차에 따라 0.1~1.0 곱
- **추천**: 아이템 인접 노드를 엣지 가중치 내림차순
- **검색**: 이름 부분일치 + 태그 일치, 노드 중심성(인접 가중치 합)으로 랭킹
- ML/외부 그래프 DB 사용하지 않음. 표준 라이브러리 + FastAPI만 사용.

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

11개 테스트 통과 (그래프 7건, API 4건).

## 성능 (참고치, 단일 프로세스)

`scripts/seed.py` 결과 (10,000 아이템 / 500,000 이벤트):

| 항목 | 값 |
|------|-----|
| ingest 처리량 | ~580k events/sec |
| recommend P50 | 0.003 ms |
| recommend P95 | 0.006 ms |
| recommend P99 | 0.008 ms |

## 한계

- 영속화 없음(메모리만). 재시작 시 그래프 손실.
- 아이템/엣지 무한 누적, 실제 운영에는 LRU/감쇠 일괄 작업 필요.
- 검색은 부분일치만 지원. n-gram/형태소 분석 미적용.
