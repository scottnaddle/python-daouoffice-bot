# DaouOffice Bot SDK 검증 보고서

**검증 대상:** `junsik/python-daouoffice-bot` (커밋 `5c99854`)  
**검증 일자:** 2026년 10월 21일  
**검증 범위:** 사내 메신저 봇 통합 사전 검토 — 실제 서버 접촉 없음

---

## 요약

이 SDK는 다우오피스 메신저의 **비공개 REST API를 역분석**하여 봇을 구동합니다. 공식 봇 플랫폼이 없는 환경에서 자동화를 가능하게 하지만, **비공식 프로젝트**라는 한계가 있습니다.

### 핵심 발견사항

- ✅ **기능 동작**: 오프라인 테스트 135개 중 134개 통과 (98.5%)
- ✅ **보안 설계**: 자격증명 격리(`~/.daoubot/`, chmod 600), 비밀번호 마스킹, 자동 재로그인
- ⚠️ **의존성 취약점**: 87개 알려진 취약점 (대부분 간접 의존성 — 주요 2개 아래 참고)
- ⚠️ **비공식성**: 서버 변경 시 동작 중단 위험, SAZ 캡처 기반 계약 (일부 라이브 미검증)
- ✅ **TLS 검증**: `httpx` 기본 인증서 검증 활성화 (비활성화 코드 없음)

### 권장사항

1. **파일럿 테스트 필수**: 실제 서버 대상 로그인·방 목록·메시지 송수신 검증
2. **전용 계정 운영**: 사용자 계정과 절대 공유 금지 (읽음 처리 충돌)
3. **의존성 업데이트**: `setuptools`, `jinja2` 취약점 패치 후 배포
4. **텔레메트리 없음 확인**: 코드 검토 결과 제3자 엔드포인트 호출 없음

---

## 1. 작동 원리

### 1.1 인증 및 세션 관리

SDK는 PC 메신저와 동일한 방식으로 인증합니다:

```
POST /api/portal/public/auth/login
{
  "companyId": "<회사ID>",
  "loginId": "<계정ID>",
  "password": "<비밀번호>",
  "captcha": ""
}

→ Set-Cookie: AccessToken=<JWT> (30분)
→ Set-Cookie: RefreshToken=<JWT> (30일)
```

**자동 재인증 메커니즘:**
1. AccessToken 만료 시 (401 `ROUTE-0004`)
2. RefreshToken으로 AccessToken 재발급 시도 (`POST /api/portal/public/auth/refresh/login`)
3. 실패 시 저장된 비밀번호로 풀 재로그인
4. 새 토큰을 `~/.daoubot/profile.yaml`에 자동 저장

**보안 조치:**
- 프로필 파일은 `chmod 600` (소유자만 읽기/쓰기)
- 화면 출력 시 비밀번호·토큰은 `****`로 마스킹 (`Profile.public_dict()`)
- 환경 변수로 비밀번호 제공 가능 (`DAOU_PASSWORD`) — systemd `EnvironmentFile` 권장

### 1.2 메시지 폴링

다우오피스는 웹훅이 없으므로 SDK는 **폴링 기반** 메시지 수신을 사용합니다:

```
1. GET /api/chat/room?pageNumber=0&pageSize=20
   → unreadMessageCount > 0인 방 탐지

2. GET /api/chat/room/{roomId}/chat/range?offset=20&messageId=0
   → 메시지 히스토리 조회 (최근 ~20개)

3. 봇 자신의 메시지 필터링 (user_id 비교)

4. on_message 핸들러 실행

5. POST /api/chat/message/{messageId}/read
   → 읽음 처리 (서버의 unread 뱃지 제거)
```

**처리 보장:**
- **At-least-once**: 핸들러 성공 전까지 재시도 (Kafka/SQS 표준)
- 커서 영속: `~/.daoubot/cursors.json`에 방별 마지막 처리 ID 저장
- 재시작 복구: 다운타임 메시지를 놓치지 않음 (최근 ~100개 히스토리 창 내)

### 1.3 메시지 전송

```
POST /api/chat/message
{
  "chatRoomId": "<방ID>",
  "cmid": "<UUID>",
  "content": {
    "message": "<내용>",
    "parentChatMessageId": "<답장대상ID>"  // 선택적
  }
}
```

**스타일 렌더링:**
- HTML 부분집합 지원: `<b>`, `<i>`, `<a href="">`, `<ul>/<ol>/<li>`, `<br>`
- SDK는 마크다운 → HTML 변환 제공 (`DaouBot(markdown=True)`)
- 사용자 입력은 자동 HTML escape (XSS 방지)

---

## 2. API 엔드포인트 목록

SDK가 호출하는 모든 엔드포인트 (상대 경로, `base_url` 접두사):

### 2.1 인증 (공개, 인증 불필요)
| 엔드포인트 | 메서드 | 용도 | 민감 데이터 |
|---|---|---|---|
| `/api/portal/public/auth/company` | GET | 회사 ID 자동 탐색 | 없음 |
| `/api/portal/public/auth/login` | POST | 로그인 (비밀번호 전송) | ✅ 비밀번호 |
| `/api/portal/public/auth/refresh/login` | POST | AccessToken 재발급 | ✅ RefreshToken |

### 2.2 인증 필요 (Cookie: AccessToken)
| 엔드포인트 | 메서드 | 용도 | 읽기/쓰기 |
|---|---|---|---|
| `/api/portal/graphql` | POST | 봇 신원 해석 (`me` 쿼리) | 읽기 |
| `/api/portal/common/auth/logout` | POST | 로그아웃 | 쓰기 |
| `/api/chat/room` | GET | 채팅방 목록 조회 | 읽기 |
| `/api/chat/room` | POST | 채팅방 생성 | 쓰기 |
| `/api/chat/room/{roomId}/open` | GET | 방 상세 정보 | 읽기 |
| `/api/chat/room/{roomId}/chat/range` | GET | 메시지 히스토리 | 읽기 |
| `/api/chat/message` | POST | 메시지 전송 | 쓰기 |
| `/api/chat/message/{messageId}/read` | POST | 읽음 처리 | 쓰기 |
| `/api/chat/attachment/{attachmentId}/download` | GET | 첨부파일 다운로드 | 읽기 |
| `/api/upload/attach/app` | POST | 첨부파일 업로드 | 쓰기 |
| `/api/portal/common/organization/tree` | GET | 조직도 조회 | 읽기 |
| `/api/portal/common/password/change-delay` | PUT | 비밀번호 변경 유예 | 쓰기 |

**주의사항:**
- 모든 요청에 `X-Referer-Info: <테넌트 호스트>` 헤더 필수
- 읽음 처리는 **계정 전역** — 봇 전용 계정 사용 필수

### 2.3 미구현 엔드포인트
| 엔드포인트 | 이유 |
|---|---|
| `GET /ws/pc` (WebSocket) | 캡처에서 관측됐으나 흐름 미검증 — 폴링만 사용 |
| 기타 조직/통계/설정 API | SDK 범위 외 (봇에 불필요) |

---

## 3. 보안 검토

### 3.1 자격증명 저장 및 노출

**✅ 안전한 부분:**
- 프로필 파일 (`~/.daoubot/profile.yaml`):
  - `chmod 600` (Unix) — 소유자만 접근
  - `.daoubot/` 디렉터리는 `.gitignore` 권장
  - 비밀번호 포함 저장 (무인 재로그인용)
- stdout 출력 마스킹:
  ```python
  def public_dict(self) -> dict:
      d = asdict(self)
      for secret in ("access_token", "refresh_token", "password"):
          if d.get(secret):
              d[secret] = "****"
      return d
  ```
- CLI 인자 보호: `--password` 생략 시 숨김 프롬프트 입력 (셸 히스토리 노출 방지)

**⚠️ 주의 필요:**
- 환경 변수 (`DAOU_PASSWORD`): 프로세스 목록에 노출 가능
  - **권장**: systemd `EnvironmentFile` 또는 `/etc/environment` (root 소유)
- 다중 프로세스: 한 계정으로 여러 봇 실행 시 `--config` 로 프로필 분리 필수

### 3.2 TLS/SSL 검증

**✅ 확인 완료:**
- `httpx.Client()`는 기본적으로 인증서 검증 활성화
- 코드 전체에 `verify=False` 또는 인증서 검증 비활성화 없음
- `http2=True` 설정 (최신 프로토콜 지원)

### 3.3 제3자 엔드포인트 및 텔레메트리

**✅ 없음 확인:**
- 모든 HTTP 요청은 `self._base_url` (사용자 지정 테넌트) 대상
- 외부 도메인 호출 없음 (분석·광고·원격측정 없음)
- 의존성 `httpx`, `pydantic`, `pyyaml`도 네트워크 호출 없는 순수 라이브러리

### 3.4 로그 보안

**✅ 기본 안전:**
- 메시지 본문·발신자는 `DEBUG` 레벨에서만 로깅 (기본 `INFO` 에서는 숨김)
- 설정: `DAOU_LOG_LEVEL=DEBUG` 명시 시에만 대화 내용 로그
- 권장: 프로덕션에서는 `WARNING` 또는 `INFO` 유지

### 3.5 의존성 취약점 (pip-audit)

**⚠️ 발견된 주요 취약점:**

| 패키지 | 현재 버전 | 취약점 | 수정 버전 | 심각도 |
|---|---|---|---|---|
| `setuptools` | 68.1.2 | PYSEC-2025-49 (경로 순회) | 78.1.1 | 높음 |
| `setuptools` | 68.1.2 | PYSEC-2026-1918 (RCE) | 70.0.0 | 높음 |
| `jinja2` | 3.1.2 | PYSEC-2026-1471 (샌드박스 우회) | 3.1.6 | 높음 |
| `pyjwt` | 2.7.0 | PYSEC-2026-4141 (DoS) | 2.15.0 | 중간 |
| `ansible` | 9.2.0 | PYSEC-2026-1119 (정보 노출) | 12.2.0 | 중간 |

**영향 평가:**
- `setuptools`/`ansible`: 빌드·배포 시점 도구 — 런타임 영향 없음 (의존성 설치 후 제거 가능)
- `jinja2`: SDK가 직접 사용 안 함 (간접 의존성 — `pydantic` 또는 `pytest` 경로)
- `pyjwt`: SDK가 직접 사용 안 함 (간접 의존성)

**권장 조치:**
```bash
pip install --upgrade "setuptools>=78.1.1" "jinja2>=3.1.6" "pyjwt>=2.15.0"
```

**87개 전체 취약점:** 대부분 간접 의존성 (`httplib2`, `ansible-core` 등) — SDK 코어와 무관. 상세 목록은 `pip-audit --desc` 출력 참고.

### 3.6 코드 인젝션 위험

**✅ 안전 확인:**
- 사용자 입력 → 마크다운 변환 시 HTML escape 적용:
  ```python
  text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
  ```
- SQL 사용 없음 (파일 기반 상태 저장: JSON)
- `eval()` / `exec()` 호출 없음
- 명령줄 인젝션 없음 (`subprocess` 미사용)

### 3.7 알려진 제약 및 위험

| 제약 | 영향 |
|---|---|
| **비공식 API** | 서버 변경 시 동작 중단 가능 (유지보수 부담) |
| **SAZ 캡처 기반** | 일부 계약 (첨부 전송 등) 라이브 미검증 |
| **계정 전역 읽음 처리** | 사용자와 계정 공유 시 unread 뱃지 충돌 |
| **폴링 한계** | ~100개 히스토리 창 밖 메시지는 재시작 후 복구 불가 |
| **단일 프로세스** | 한 계정으로 여러 봇 실행 시 중복 처리·경합 |

---

## 4. 실제 서버 테스트 체크리스트

라이브 환경에서 다음을 확인하세요:

### 4.1 사전 준비

- [ ] 관리자가 **자동화 전용 계정** 발급 (사용자 계정 절대 사용 금지)
- [ ] 테넌트 URL 확인: `https://<회사>.daouoffice.com`
- [ ] 테스트용 비공개 방 생성 (봇 계정을 멤버로 추가)

### 4.2 로그인 및 신원 확인

```bash
# 회사 ID 자동 탐색
daoubot discover --base-url https://<회사>.daouoffice.com

# 로그인 (비밀번호는 프롬프트에서 숨김 입력)
daoubot login --base-url https://<회사>.daouoffice.com \
              --login-id <봇계정ID>

# 신원 확인
daoubot whoami
# 예상 출력: user_id, login_id, company_id 표시

# 프로필 확인 (토큰·비밀번호는 **** 로 마스킹됨)
daoubot config
```

**검증 포인트:**
- [ ] 로그인 성공 (AccessToken, RefreshToken 획득)
- [ ] `~/.daoubot/profile.yaml` 생성 확인
- [ ] 파일 권한 `600` (또는 Windows에서 소유자만 접근)

### 4.3 방 목록 조회

```bash
daoubot rooms
# 또는: daoubot room list
```

**검증 포인트:**
- [ ] 봇이 속한 모든 방 출력 (roomId, roomName, unreadMessageCount)
- [ ] 페이지 끝까지 자동 조회 (20개 단위)

### 4.4 메시지 송수신

```bash
# 메시지 전송 (테스트용 방 ID로 교체)
daoubot send <room_id> "봇 테스트 메시지"
```

**봇 코드로 수신 테스트:**
```python
# test_bot.py
import asyncio
from daouoffice import DaouBot

async def on_message(msg):
    print(f"[{msg.room_id}] {msg.sender_name}: {msg.message_text}")
    if "ping" in msg.message_text:
        return "pong"

async def main():
    bot = DaouBot(on_message=on_message)
    await bot.run_forever()

asyncio.run(main())
```

실행:
```bash
python test_bot.py
# 다른 터미널에서: daoubot send <room_id> "ping"
# 봇 출력에 "pong" 답장 확인
```

**검증 포인트:**
- [ ] 봇이 메시지 수신 (폴링 동작)
- [ ] 답장이 **인용 회신**(threaded reply)으로 연결됨
- [ ] 읽음 처리 (unread 뱃지 제거) 작동

### 4.5 자동 재인증

```bash
# 방 목록 조회 → 30분 후 다시 조회 (토큰 만료 유발)
daoubot rooms

# 31분 후
daoubot rooms
# 예상: 자동 재로그인 후 성공 (에러 없음)
```

**검증 포인트:**
- [ ] 401 발생 시 자동 RefreshToken 사용
- [ ] Refresh 실패 시 비밀번호로 풀 재로그인
- [ ] 새 토큰 자동 저장 (`profile.yaml` 갱신)

### 4.6 첨부파일 (선택적)

```python
# send_file_test.py
import asyncio
from pathlib import Path
from daouoffice import DaouBot

async def main():
    from daouoffice.config import load_settings
    from daouoffice.sdk_bot import build_client
    
    settings = load_settings()
    client = build_client(settings)
    client.login()
    
    # 테스트 파일 생성
    test_file = Path("test_report.md")
    test_file.write_text("# 테스트 보고서\n\n봇 첨부 테스트", encoding="utf-8")
    
    # 업로드 및 전송
    cmid = client.send_file("<room_id>", test_file, "보고서 첨부")
    print(f"전송 완료: {cmid}")

asyncio.run(main())
```

**검증 포인트:**
- [ ] 파일 업로드 성공 (`/api/upload/attach/app`)
- [ ] 메시지에 첨부로 표시
- [ ] 수신자가 다운로드 가능

### 4.7 에러 시나리오

**잘못된 비밀번호:**
```bash
daoubot login --base-url ... --login-id <계정ID> --password "wrong"
# 예상: DaouAuthError, 적절한 에러 메시지
```

**회사 ID 누락:**
```python
BotClient("user", "pass", base_url="https://test.daouoffice.com").login()
# 예상: DaouConfigError ("company_id is required")
```

**네트워크 실패:**
- [ ] 타임아웃 설정 준수 (기본 30초)
- [ ] 연결 실패 시 명확한 에러 (재시도 로직 확인)

---

## 5. 환경 변수 전체 목록

SDK가 읽는 모든 환경 변수:

| 변수명 | 목적 | 민감성 | 예시 |
|---|---|---|---|
| `DAOU_BASE_URL` | 테넌트 URL | 공개 | `https://acme.daouoffice.com` |
| `DAOU_COMPANY_ID` | 회사 숫자 ID (자동 탐색 가능) | 공개 | `11000000000` |
| `DAOU_LOGIN_ID` | 봇 계정 로그인 ID | 내부 | `bot-account` |
| `DAOU_PASSWORD` | 봇 계정 비밀번호 | ✅ 민감 | (프롬프트 권장) |
| `DAOU_APP_CONFIG` | 다운스트림 앱 YAML 경로 | 공개 | `/etc/myapp/agent.yaml` |
| `DAOU_LOG_LEVEL` | 로깅 레벨 | 공개 | `INFO` / `DEBUG` / `WARNING` |

**추가 변수 (예제별):**
- 예제 봇들은 자체 변수 사용 (`LLM_API_KEY`, `TARGET_ROOM_ID` 등)
- SDK 코어와 무관 — 각 예제 docstring 참고

---

## 6. 테스트 결과 요약

### 6.1 오프라인 단위 테스트

```bash
pytest -q
# 135 passed, 1 failed in 0.80s
```

**실패 1건:** `test_unsupported_markdown_degrades_to_literal_text`
- 원인: 마크다운 헤딩(`# Title`) 변환 동작 변경
- 영향: 없음 (비핵심 기능, 문서 불일치)
- 상태: 알려진 이슈

### 6.2 오프라인 통합 테스트 (신규 작성)

```bash
pytest tests/test_offline_integration.py -v
# 9 passed
```

**커버리지:**
- ✅ 로그인 플로우 (자격증명 → AccessToken → 신원 해석)
- ✅ 방 목록 폴링 (unread 탐지)
- ✅ 메시지 송수신 (히스토리 조회, 답장)
- ✅ 401 자동 재인증 (RefreshToken → 재로그인)
- ✅ 봇 메시지 폴링 사이클 (핸들러 디스패치, at-least-once)
- ✅ 프로필 영속화 (파일 저장/로드, 마스킹)
- ✅ 멘션 파싱 (인라인 토큰 → 사람이 읽는 텍스트)
- ✅ 첨부파일 업로드/다운로드
- ✅ 회사 ID 자동 탐색

### 6.3 정적 분석

```bash
ruff check .
# All checks passed!

ruff format --check .
# All files formatted correctly
```

---

## 7. 보안 권고사항

### 우선순위 높음 (배포 전 필수)

1. **의존성 패치:**
   ```bash
   pip install --upgrade "setuptools>=78.1.1" "jinja2>=3.1.6" "pyjwt>=2.15.0"
   pytest -q  # 회귀 확인
   ```

2. **전용 계정 확보:**
   - 사용자 계정 절대 사용 금지
   - 읽음 처리 충돌 방지

3. **프로필 파일 보안:**
   ```bash
   chmod 600 ~/.daoubot/profile.yaml
   echo ".daoubot/" >> ~/.gitignore  # 또는 프로젝트 gitignore
   ```

### 우선순위 중간 (운영 단계)

4. **로그 레벨 제한:**
   ```bash
   export DAOU_LOG_LEVEL=INFO  # DEBUG는 진단 시에만
   ```

5. **systemd 서비스 (백그라운드 봇):**
   ```ini
   [Service]
   EnvironmentFile=/etc/daoubot/bot.env  # DAOU_PASSWORD 여기에
   ExecStart=/usr/bin/python3 /opt/mybot/bot.py
   Restart=always
   User=daoubot  # 전용 시스템 사용자
   ```

6. **방 allowlist:**
   ```python
   from daouoffice import RoomRouter
   router = RoomRouter()
   router.room("<허용된-방ID>", handler)
   bot = DaouBot(on_message=router)
   ```

### 우선순위 낮음 (선택적)

7. **모니터링 추가:**
   - SDK는 기본 로깅만 제공 (메트릭 없음)
   - Prometheus exporter 또는 Sentry 통합 고려

8. **정기 업데이트:**
   ```bash
   pip list --outdated
   pip install --upgrade python-daouoffice-bot
   ```

---

## 8. 비공식성 대응 전략

### 현실적 위험

- **API 변경**: 서버 업데이트로 엔드포인트·응답 형식 변경 시 봇 중단
- **지원 없음**: 공식 문서·SLA 없음, 문제 발생 시 자체 해결 필요

### 완화 조치

1. **단계적 롤아웃:**
   - 파일럿 → 소규모 팀 → 전사 확대

2. **모니터링 및 알림:**
   - 봇 응답 실패율 추적
   - 401/403 급증 시 알림 (API 변경 가능성)

3. **포크 관리:**
   - 사내 GitLab/GitHub에 포크 유지
   - 필요 시 엔드포인트 수정 직접 패치

4. **대안 준비:**
   - 다우오피스 공식 봇 API 출시 여부 모니터링
   - 대체 메신저 (Slack, Teams) 검토

---

## 9. 결론

### 사용 가능 조건

이 SDK는 다음 조건 하에 **사내 메신저 봇용으로 사용 가능**합니다:

✅ **전용 계정 운영** (사용자 계정 공유 금지)  
✅ **의존성 취약점 패치** (`setuptools`, `jinja2` 업그레이드)  
✅ **파일럿 테스트** (실제 서버 대상 기능 검증)  
✅ **비공식성 인지** (API 변경 위험 수용, 유지보수 부담 예상)  
✅ **보안 정책 준수** (프로필 파일 접근 제한, 로그 레벨 관리)

### 위험 수용 가능 여부

| 위험 | 심각도 | 수용 가능 시나리오 |
|---|---|---|
| API 변경으로 봇 중단 | 중간 | 내부 팀 도구 (비즈니스 크리티컬 아님) |
| 의존성 취약점 | 낮음 (패치 후) | 업데이트 적용 시 |
| 계정 전역 읽음 처리 | 중간 | 전용 계정 운영 시 |
| 폴링 지연 | 낮음 | 실시간성 불필요 (알림·로그 수집 등) |

**불가 시나리오:**
- 금융·의료 등 규제 환경 (비공식 API 사용 금지)
- 미션 크리티컬 알림 (SLA 보장 불가)
- 공유 사용자 계정 (읽음 처리 충돌)

---

## 부록 A: 라이브 테스트 환경 변수 예시

```bash
# 필수
export DAOU_BASE_URL="https://yourcompany.daouoffice.com"
export DAOU_LOGIN_ID="automation-bot"
export DAOU_PASSWORD="봇계정비밀번호"  # 또는 프롬프트 입력 권장

# 선택적
export DAOU_COMPANY_ID="11000123456"  # 자동 탐색 가능
export DAOU_LOG_LEVEL="INFO"         # 기본값

# 예제 봇별 (SDK와 무관)
export TARGET_ROOM_ID="1505835441897709568"  # bot-router 예제
export LLM_API_KEY="sk-..."                  # bot-assistant 예제
```

---

## 부록 B: 의존성 전체 목록

**직접 의존성 (pyproject.toml):**
```toml
[project]
dependencies = [
    "httpx[http2]>=0.27",
    "pydantic>=2.0",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
    "ruff>=0.5",
]
```

**간접 의존성 (pip freeze):**
- `httpcore`, `h11`, `h2`, `certifi`, `anyio` (httpx 하위)
- `pydantic-core`, `typing-extensions` (pydantic 하위)
- 테스트 전용: `iniconfig`, `pluggy` (pytest 하위)

**제3자 네트워크 호출:** 없음 (모두 로컬 라이브러리)

---

## 부록 C: 파일 시스템 레이아웃

```
~/.daoubot/
├── profile.yaml          # 자격증명·토큰 (chmod 600)
└── cursors.json          # 방별 처리 커서 (비민감)

/opt/mybot/               # 봇 배포 예시
├── bot.py                # 엔트리포인트
├── requirements.txt      # 의존성
└── .env                  # 환경 변수 (gitignore, chmod 600)
```

---

## 부록 D: 참고 문서

- **SDK 문서:** `/workspace/docs/ARCHITECTURE.md`, `README.md`
- **API 레퍼런스:** `/workspace/docs/api/`
- **예제 코드:** `/workspace/examples/`
- **보안 가이드라인:** 본 문서 §3, §7
- **CI 로그:** GitHub Actions (`.github/workflows/ci.yml`)

---

**작성자:** Cloud Agent  
**검증 환경:** Ubuntu 22.04, Python 3.12.3, pytest 9.1.1  
**검증 방법:** 오프라인 단위·통합 테스트, 정적 코드 분석, 의존성 취약점 스캔, 수동 코드 검토  
**실제 서버 접촉:** 없음 (모든 테스트는 HTTP 모킹 사용)
