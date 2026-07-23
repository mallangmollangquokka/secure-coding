# Phase 2B + 2C 통합 지시서

You are working on the `mallangmollangquokka/secure-coding` Flask repo (Tiny Second-hand Shopping Platform).
Phase 1과 Phase 2A는 완료·커밋·배포되어 있음 (최신 커밋 `7d2bdb7`).

**이번이 마지막 구현 단계다.** Phase 2B(신고 자동조치 + 관리자)와 Phase 2C(송금 + 1:1 채팅 + 채팅 보안 강화)를 한 번에 진행한다.

---

## 0단계. 시작 전 필수 확인 (코드 작성 금지)

아래 파일을 **먼저 전부 읽고** 현재 상태를 파악한 뒤에 구현을 시작할 것.

- `app.py` 전체
- `security.py` 전체
- `templates/base.html`, `dashboard.html`, `report.html`, `view_product.html`, `profile.html`
- `static/js/chat.js`
- `spec.md`, `phase2a_prompt.md`
- `sqlite3 market.db "PRAGMA table_info(user); PRAGMA table_info(product); PRAGMA table_info(report);"`

읽은 뒤, **구현 시작 전에** 아래 3가지를 보고할 것:
1. `static/js/chat.js`가 서버로 보내는 페이로드 구조와 화면에 렌더링하는 필드 (4단계에서 이 파일을 수정해야 함)
2. `templates/dashboard.html`의 채팅 영역 DOM 구조 (`#chat`, `#messages` 등 id/class)
3. 현재 `report` 테이블의 행 수 (`SELECT COUNT(*) FROM report`)

---

## 반드시 지킬 원칙 (위반 시 전면 재작업)

이건 Phase 1·2A에서 이미 확립된 것이고, 여기서 깨지면 기존 기능이 무너진다.

### 🔴 최우선: `log_action()`을 트랜잭션 중간에 호출하지 말 것

`security.py::log_action()`은 마지막에 `db.commit()`을 호출하고, `security.py::_get_db()`는 `app.py::get_db()`와 **동일한 `g._database` 커넥션을 공유**한다.

즉 트랜잭션 진행 중에 `log_action()`을 부르면 **그 시점까지의 부분 작업이 커밋되고 트랜잭션이 끊긴다.** 송금(2단계)에서 이걸 어기면 "돈은 빠져나갔는데 상대에게 안 들어가는" 치명적 버그가 되고, 롤백도 불가능해진다.

**규칙: 모든 `log_action()` 호출은 해당 작업의 최종 `db.commit()` 이후에 배치할 것.** 예외 없음.

### 그 외 절대 금지

- **인라인 스크립트/이벤트 핸들러 금지.** `<script>...</script>`, `onclick=""`, `onsubmit=""` 전부 금지. 새 JS는 반드시 `static/js/*.js`로 분리하고 `<script src="...">`로 로드. (Phase 1에서 이걸 어겨 채팅이 통째로 마비된 이력 있음)
- **CSP에 `unsafe-inline` 추가 금지.** `base.html`이 인라인 `<style>`을 쓰고 있으므로 `default-src`를 새로 추가하지도 말 것 — 스타일이 전부 죽는다.
- **`|safe` 필터 금지.** 저장은 원본 그대로, 이스케이프는 렌더링 시점 Jinja2 autoescape에만 맡길 것. 저장 전 이스케이프 금지(이중 인코딩 버그).
- **날짜/시간은 반드시** `datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')` — 기존 `utcnow_str()` / `_utcnow_str()` 재사용.
- **비밀번호·해시·session_token은 어디에도 로깅 금지** (`audit_log`, `admin_action_log` 포함).
- **`ALTER TABLE ADD COLUMN`은 구문마다 개별 `try/except sqlite3.OperationalError`.**
- **`init_db()` 전체를 블랭킷 try/except로 감싸지 말 것.**
- **CSS 프레임워크(Bootstrap/Tailwind) 도입 금지.** `base.html`의 인라인 `<style>` 블록만 확장.
- **모든 상태변경은 POST + `{{ csrf_token() }}`.** GET으로 상태 바꾸는 라우트 금지.
  - 단, **Socket.IO 이벤트는 이 규칙의 대상이 아니다.** Flask-WTF의 `CSRFProtect`는 HTTP 요청만 가로채므로 소켓 이벤트에는 애초에 적용되지 않는다. 소켓 핸들러에 `@csrf.exempt`를 붙이거나 CSRF 토큰을 소켓 페이로드에 실어 보내는 식의 처리를 **추가하지 말 것**(불필요하고, 잘못 건드리면 채팅이 통째로 죽는다). 소켓의 방어는 4-1의 `socket_user_or_none()` 세션·상태 재검증이 담당한다.
- **코드 생략 표기 금지.** `# ... 기존 코드 유지 ...`, `(생략)`, `# 이하 동일` 전부 금지. 변경된 함수/파일은 전체를 온전히 출력.

---

## 진행 순서 (반드시 이 순서대로, 단계마다 검증 후 다음으로)

각 단계가 끝날 때마다 **그 단계의 자체 검증을 실제로 실행하고 결과를 보고한 뒤** 다음 단계로 넘어갈 것. 전부 만들고 마지막에 한꺼번에 검증하면 어디서 깨졌는지 못 찾는다.

---

# 1단계. DB 마이그레이션 (전부 여기서 끝낸다)

이후 단계에서 스키마를 다시 손대지 않도록 필요한 것을 한 번에 추가한다. 전부 `init_db()` 안, 기존 마이그레이션 블록 바로 뒤에 배치.

### 1-1. `user` 테이블 — 누락 컬럼 2개 추가

**중요: 아래 두 컬럼은 spec.md §1에 있으나 실제로는 아직 추가되지 않은 상태다.** 송금과 신고 자동조치의 핵심 컬럼이므로 반드시 추가할 것.

```sql
ALTER TABLE user ADD COLUMN balance INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user ADD COLUMN report_count INTEGER NOT NULL DEFAULT 0;
```
(각각 개별 try/except)

### 1-2. `report` 테이블 확장

```sql
ALTER TABLE report ADD COLUMN target_type TEXT NOT NULL DEFAULT 'product';
ALTER TABLE report ADD COLUMN created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE report ADD COLUMN auto_action_taken INTEGER NOT NULL DEFAULT 0;
ALTER TABLE report ADD COLUMN auto_action_at TEXT DEFAULT NULL;
```

**spec.md §3-3의 가드 조항에 대한 결정(이 지시서가 spec을 대체함):**
spec §3-3은 "`SELECT COUNT(*) FROM report`가 0이 아니면 스크립트 중단"이었으나, `init_db()`는 모듈 최상단에서 실행되므로 여기서 중단하면 **배포 서버 전체가 기동 실패**한다. 따라서:

- `ALTER TABLE ADD COLUMN ... DEFAULT 'product'`는 **비파괴적**이다(기존 데이터 손실 없음, 기본 라벨만 부여됨). 그러므로 중단하지 말고 진행한다.
- 다만 마이그레이션 직전에 `SELECT COUNT(*) FROM report`를 세고, **0이 아니면 `print()`로 경고를 남길 것**: `"[MIGRATION WARNING] 기존 report N건에 target_type='product' 기본값이 부여됨. 실제 대상 종류 수동 확인 필요."`
- 이 결정은 spec §3-3에서 의도적으로 벗어난 것이므로 보고서에 기록할 수 있도록 **최종 보고에 명시**할 것.

### 1-3. 중복 신고 방지용 UNIQUE 인덱스

SQLite는 `ALTER TABLE`로 UNIQUE 제약을 추가할 수 없으므로 인덱스로 처리:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_report_unique
    ON report (reporter_id, target_type, target_id);
```

기존 데이터에 중복이 있으면 인덱스 생성이 실패하므로 **try/except로 감싸고**, 실패 시 `print()`로 경고만 남기고 진행할 것(애플리케이션 레벨 중복 체크가 5단계에 별도로 들어가므로 인덱스는 이중 안전장치다).

### 1-4. 신규 테이블 4개

```sql
CREATE TABLE IF NOT EXISTS admin_action_log (
    id TEXT PRIMARY KEY,
    actor_type TEXT NOT NULL,       -- 'system' | 'admin'
    actor_id TEXT,                  -- admin 수동조치일 때만 관리자 user.id
    action TEXT NOT NULL,           -- 'block_product' | 'suspend_user' | 'restore_product' | 'unsuspend_user' | 'grant_balance'
    target_type TEXT NOT NULL,      -- 'user' | 'product'
    target_id TEXT NOT NULL,
    reason TEXT,                    -- 관련 report.id 또는 사유
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS direct_message (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS "transaction" (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS global_message (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    sender_username TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

⚠️ **`transaction`은 SQL 예약어다.** 이 테이블을 참조하는 **모든 쿼리에서 반드시 큰따옴표로 감쌀 것**: `INSERT INTO "transaction" ...`, `SELECT * FROM "transaction" ...`. 따옴표를 빠뜨리면 `sqlite3.OperationalError: near "transaction": syntax error`가 난다.

### ✅ 1단계 자체 검증
- `python app.py` 기동 → 에러 없음
- `PRAGMA table_info(user)`에 `balance`, `report_count` 존재
- `PRAGMA table_info(report)`에 `target_type`, `created_at`, `auto_action_taken`, `auto_action_at` 존재
- `.tables`에 `admin_action_log`, `direct_message`, `transaction`, `global_message` 존재
- 서버 재기동 2회 → 에러 없고 기존 데이터(user/product/audit_log 행 수) 유지
- **예약어 누락 검사**: 구현 완료 후 `grep -n "transaction" app.py security.py`로 전수 확인해, `"transaction"`처럼 큰따옴표로 감싸지지 않은 채 SQL 문자열 안에 쓰인 곳이 하나도 없는지 확인하고 결과를 보고할 것

---

# 2단계. 송금 (`/transfer`) — 가장 틀리기 쉬운 지점

### 구현

`POST /transfer`, `@login_required`, `@limiter.limit('10 per hour', methods=['POST'])`

폼 필드: `receiver_username`, `amount`, `current_password`

**검증 순서 (전부 DB 트랜잭션 시작 *전에* 수행):**
1. `current_password`를 bcrypt로 검증 — **민감 작업 재인증**(spec §3-12). 틀리면 실패 처리
2. `amount`를 int 파싱, 실패하거나 `<= 0`이면 거부
3. `receiver_username`으로 수신자 조회 → 없거나 `status != 'active'`면 거부
4. 수신자가 본인이면 거부 ("자기 자신에게는 송금할 수 없습니다")

**트랜잭션 (원자성 필수, spec §3-1-A):**

```python
db.execute("BEGIN IMMEDIATE")
cursor.execute(
    "UPDATE user SET balance = balance - ? WHERE id = ? AND balance >= ?",
    (amount, sender_id, amount)
)
if cursor.rowcount == 0:
    db.rollback()
    # 잔액 부족 → flash 후 redirect
cursor.execute("UPDATE user SET balance = balance + ? WHERE id = ?", (amount, receiver_id))
cursor.execute('INSERT INTO "transaction" (id, sender_id, receiver_id, amount, created_at) VALUES (?, ?, ?, ?, ?)', ...)
db.commit()
```

- **잔액 부족 판정은 예외가 아니라 `cursor.rowcount == 0`으로 한다.** 조건절이 매칭되지 않은 것이므로 예외가 발생하지 않는다.
- 차감 → 증가 → INSERT 세 개가 **반드시 하나의 트랜잭션**이어야 한다.
- 🔴 **트랜잭션 안에서 `log_action()`을 호출하지 말 것.** `db.commit()` 이후에 호출.
- 예외 발생 시 `db.rollback()` 후 500이 아니라 flash + redirect로 처리.

`log_action('transfer', target_type='user', target_id=receiver_id, success=True)` — **금액은 audit_log에 기록해도 무방하나, 비밀번호는 절대 금지.**

### 템플릿
- `templates/transfer.html` 신규 (`base.html` 상속, CSRF 토큰, 현재 잔액 표시)
- `GET /transfer`로 폼 렌더링
- `dashboard.html`에 현재 잔액 표시 + 송금 페이지 링크

### ✅ 2단계 자체 검증
- 정상 송금 → 양쪽 잔액 정확히 반영, `"transaction"` 테이블에 1행
- 잔액 초과 송금 → 거부, **양쪽 잔액 변동 없음**
- `current_password` 틀림 → 거부, 잔액 변동 없음
- `amount`에 `0`, `-100`, `abc` 입력 → 전부 거부
- 자기 자신에게 송금 → 거부
- **동시성 테스트**: 잔액 1000인 계정에서 `threading`으로 800원 송금 2건 동시 요청 → 정확히 1건만 성공, 잔액 음수 없음, `"transaction"` 행 수 1
- 송금 전후 `SELECT SUM(balance) FROM user` 총합 보존 확인

---

# 3단계. 1:1 채팅 (`direct_message` + Socket.IO room)

### 라우트
- `GET /messages` — 내가 주고받은 상대 목록
- `GET /message/<user_id>` — 특정 상대와의 대화 스레드. `@login_required`. 상대가 존재하고 `status='active'`인지 확인, 아니면 404. `direct_message`에서 `(sender_id=me AND receiver_id=them) OR (sender_id=them AND receiver_id=me)`를 `created_at ASC`로 조회
- `templates/dm_thread.html`, `templates/dm_list.html` 신규

### 진입점 UI (누락하면 기능에 도달할 수 없음)
라우트만 만들면 사용자가 주소창에 상대 id를 직접 입력해야 하므로 반드시 링크를 추가할 것:
- `templates/view_product.html` — 판매자 username 옆에 `[1:1 문의하기]` 버튼 (`url_for('dm_thread', user_id=seller.id)`). **본인 상품일 때는 노출하지 않음**
- `templates/user_profile.html` — 프로필 상단에 `[1:1 채팅하기]` 버튼. **자기 자신의 프로필에서는 노출하지 않음**
- `templates/base.html` nav — 로그인 상태일 때 `[쪽지함]`(`/messages`) 링크 추가
- 비로그인 상태에서 위 버튼을 눌러 접근하면 `login_required`가 로그인 페이지로 보냄(정상 동작)

### room 명명 규칙
두 user_id를 **정렬 후 결합**: `dm_{min(a,b)}_{max(a,b)}`. 유추 가능한 값이므로 **서버측 검증이 유일한 방어선이다.**

### Socket.IO 핸들러 (`static/js/dm.js`로 클라이언트 분리)

**`join_dm` 이벤트:**
```
1. 세션에서 user_id 확인 (없으면 return)
2. DB 조회 → 해당 유저 status == 'active' 이고 session_token 일치 확인 (3-1 참고)
3. 요청된 room_id를 파싱해서 두 참여자 id 추출
4. 세션 user_id가 두 참여자 중 하나인지 서버측 검증
   → 아니면 join_room() 호출하지 말고 즉시 return + log_action('dm_join_denied', success=0) 기록
5. 통과 시에만 join_room(room_id)
```

**`send_dm` 이벤트:**
- **join 시점 검증을 신뢰하지 말고 매번 위 1~4를 반복 검증할 것.** join 이후에 계정이 정지될 수 있다.
- 메시지 검증: 문자열 여부, `strip()` 후 비어있지 않음, 500자 이하
- Rate limit 적용 (4-2 참고)
- `direct_message`에 INSERT (원본 그대로 저장, 이스케이프 금지)
- **broadcast 페이로드는 서버가 세션 기준으로 새로 구성** (4-3 참고)
- `emit(..., room=room_id)` — 전체 broadcast 아님

### ✅ 3단계 자체 검증
- A↔B 대화 정상 송수신, 새로고침해도 이력 유지
- **C 계정으로 A↔B의 room에 `join_dm` 시도 → 조인 거부되고 이후 그 방 메시지를 수신하지 못함**
- C가 A↔B room으로 `send_dm` 직접 emit → 저장·전달 안 됨
- 비로그인 소켓으로 `join_dm`/`send_dm` → 차단
- 501자 메시지 → 거부

---

# 4단계. 전체 채팅 보안 강화 + DB 저장

현재 `handle_send_message_event`에는 아래 3가지 결함이 있다. 전부 고칠 것.

### 4-1. 🔴 소켓 핸들러의 계정 상태 재검증 누락

현재는 `if 'user_id' not in session: return`만 확인한다. `login_required`는 매 요청마다 DB에서 `status`와 `session_token`을 재검증하는데 소켓은 하지 않으므로, **정지당한 유저가 계속 채팅할 수 있다**(handoff §3-1-B 위반).

**왜 DB 재조회가 반드시 필요한가 (이 검증을 생략하거나 세션 값만 믿도록 "최적화"하지 말 것):**
Flask-SocketIO 핸들러 안의 `session`은 **소켓 연결(handshake) 시점의 세션 사본**이다. 연결이 유지되는 동안 HTTP 쪽에서 로그아웃·비밀번호 변경·계정 정지가 일어나도 소켓이 보고 있는 세션 값은 갱신되지 않는다. 따라서 세션의 `user_id`/`session_token`은 "주장"일 뿐이고, **매 이벤트마다 DB와 대조해야만** 실시간 무효화가 성립한다.

`security.py`에 헬퍼를 추가하고 **모든 소켓 핸들러 진입부에서 호출**할 것:

```python
def socket_user_or_none():
    """소켓 핸들러용 인증 확인. 세션 user_id + DB status='active' + session_token 일치를
    모두 만족할 때만 user Row를 반환, 아니면 None."""
```

**DB 커넥션 처리 주의:**
`security.py::_get_db()`는 `g._database`에 커넥션을 캐싱하고, `app.py::close_connection`이 `@app.teardown_appcontext`로 이를 닫는다. Flask-SocketIO는 이벤트마다 request/app 컨텍스트를 푸시·팝하므로 원칙적으로는 정상 동작하지만, **실제로 그런지 직접 확인할 것**: 소켓 메시지를 30회 이상 연속 전송한 뒤 `sqlite3.OperationalError`나 `database is locked` 계열 에러가 나지 않는지, 프로세스의 열린 파일 핸들이 계속 증가하지 않는지 확인하고 결과를 보고할 것. 문제가 확인되면 소켓 핸들러 안에서는 `g` 캐싱을 쓰지 말고 로컬 커넥션을 열어 `try/finally`로 확실히 닫는 방식으로 바꿀 것.

### 4-2. 🔴 채팅 Rate Limiting 미구현 (체크리스트 항목)

Flask-Limiter는 HTTP 라우트에만 걸려 있고 소켓에는 없다. 체크리스트 "채팅 Rate Limiting"은 별도 항목이므로 반드시 구현할 것.

- 모듈 레벨 dict로 `{user_id: [timestamp, ...]}` 유지
- **5초 이내 5회 초과 전송 시 무시**하고 해당 발신자에게만 경고 emit
- `send_message`와 `send_dm` **양쪽 모두**에 적용
- **메모리 누수 방지 (2단계로 정리할 것)**:
  1. 이벤트가 들어올 때마다 해당 유저의 리스트에서 윈도우(5초)를 벗어난 타임스탬프를 리스트 컴프리헨션으로 즉시 제거
  2. 정리 후 **리스트가 비면 그 `user_id` 키 자체를 dict에서 `del`** 할 것 — 타임스탬프만 비우고 키를 남겨두면, 한 번 접속하고 다시 오지 않는 유저의 키가 프로세스 수명 내내 누적된다(이쪽이 실제 누수 지점이다)
- 단일 워커(`-w 1`) 환경이라 in-memory로 충분함

### 4-3. 🔴 클라이언트 페이로드를 그대로 broadcast (신원 위조)

현재 코드는 클라이언트가 보낸 `data` dict를 그대로 broadcast한다. `message` 외의 키는 검증되지 않으므로, 공격자가 `{message:"...", username:"admin"}`을 보내면 **관리자를 사칭한 메시지가 전체에 뿌려진다.**

**수정: 클라이언트 dict를 재사용하지 말고 서버가 새 dict를 구성할 것.**

```python
payload = {
    'message_id': str(uuid.uuid4()),
    'username': user['username'],   # 세션 기준 DB 조회값. 클라이언트 입력 절대 사용 금지
    'message': message,             # 검증 통과한 문자열만
    'created_at': utcnow_str(),
}
```
클라이언트가 보낸 다른 키는 전부 버릴 것.

### 4-4. 전체 채팅 DB 저장

- 검증 통과한 메시지를 `global_message`에 INSERT (원본 저장)
- `GET /dashboard`(또는 채팅이 렌더링되는 라우트)에서 최근 50건을 `created_at ASC`로 조회해 템플릿에 전달
- 템플릿에서 Jinja2 autoescape로 렌더링 (`|safe` 금지)
- `static/js/chat.js` 수정: 서버가 보내는 새 페이로드 구조(`username`/`message`/`created_at`)에 맞게 렌더링. **DOM 삽입은 `innerHTML`이 아니라 `textContent`를 사용할 것** (클라이언트측 XSS 방지)

### ✅ 4단계 자체 검증
- 전체 채팅 정상 송수신, **새로고침 후에도 이전 메시지 표시**
- 클라이언트에서 `{message:"x", username:"admin"}` 강제 emit → **수신측에 실제 발신자 username으로 표시됨**(admin 아님)
- 5초에 6회 연속 전송 → 6번째부터 무시됨
- 관리자가 유저를 정지시킨 뒤, **그 유저가 열어둔 소켓으로 메시지 전송 → 차단됨**
- 메시지에 `<script>alert(1)</script>` 전송 → 화면에 문자열로 표시되고 실행 안 됨, DB에는 원본 그대로 저장

---

# 5단계. 신고 확장 + 자동 차단

### 5-1. `report()` 라우트 보강

현재 `target_id`/`reason` 검증이 전혀 없다. 아래를 추가:

- `target_type` 폼 필드 추가 (`user` | `product` 라디오). 값이 둘 중 하나가 아니면 400
- `security.py`에 `validate_report_reason(reason)` 추가 — **`reason.strip()`을 먼저 적용한 뒤** 1~500자 검증. 스페이스바만 입력한 `"     "`가 길이 검증을 통과해 내용 없는 신고가 쌓이는 것을 막기 위함. DB에는 strip된 값을 저장
- **동일 문제가 Phase 2A에서 이미 나간 validator에도 있다**: `validate_product_title`, `validate_product_description`이 strip 없이 `len()`만 검사하므로 공백만으로 된 제목·설명이 통과한다. 두 함수도 strip 후 검증하도록 수정하고, 저장 값도 strip된 값으로 통일할 것 (앞뒤 공백 제거는 정규화이지 이스케이프가 아니므로 "저장은 원본 그대로" 원칙과 충돌하지 않음)
- `target_id`가 해당 타입 테이블에 **실제로 존재하는지** 확인, 없으면 400
- **자기 신고 방지**: `target_type='user'`이고 `target_id == session['user_id']`면 거부. `target_type='product'`이고 그 상품의 `seller_id == session['user_id']`면 거부
- **중복 신고 방지**: 동일 `(reporter_id, target_type, target_id)` 조합이 이미 있으면 거부 ("이미 신고한 대상입니다"). 애플리케이션 레벨 체크 + 1-3의 UNIQUE 인덱스 이중 방어. `sqlite3.IntegrityError`도 catch해서 경합 상황 방어
- `created_at` 채워서 INSERT
- `templates/report.html`에 `target_type` 라디오 추가 (`.radio-group` 클래스)
- 상품 상세 페이지에 "이 상품 신고" 링크(`target_type=product`, `target_id` 미리 채워짐), 공개 프로필에 "이 사용자 신고" 링크 추가

### 5-2. 자동 조치

신고 INSERT 성공 후 같은 트랜잭션에서:

1. 대상의 `report_count`를 +1
2. `report_count >= 5`이고 아직 조치 전이면:
   - `target_type='product'` → `UPDATE product SET status='blocked'`
   - `target_type='user'` → `UPDATE user SET status='suspended'`, **그리고 `session_token = NULL`로 갱신해 기존 세션 즉시 무효화**
3. 해당 `report` 행의 `auto_action_taken=1`, `auto_action_at=<현재시각>`
4. `admin_action_log`에 `actor_type='system'`, `action='block_product'` 또는 `'suspend_user'`, `reason=<report.id>`로 기록
5. `db.commit()`
6. 🔴 **커밋 이후에** `log_action('report_create', ...)` 호출

임계값은 상수 `REPORT_THRESHOLD = 5`로 `security.py`에 정의.

### ✅ 5단계 자체 검증
- 정상 신고 접수 → `report` 테이블에 `target_type`, `created_at` 포함해 저장
- 자기 자신 신고 → 거부
- 본인 상품 신고 → 거부
- 동일 대상 재신고 → 거부
- 존재하지 않는 `target_id` → 400
- 501자 `reason` → 거부
- **서로 다른 5개 계정으로 한 상품 신고 → 5번째에 `status='blocked'`, 목록/상세에서 404, `admin_action_log`에 `actor_type='system'` 1행**
- 유저 5회 신고 → `status='suspended'`, 그 유저가 열어둔 세션이 다음 요청에서 로그인 페이지로 튕김

---

# 6단계. 관리자 기능

### 6-1. `admin_required` 데코레이터 (`security.py`)
- `login_required` 뒤에 적용
- DB에서 현재 유저 `role`을 조회해 `'admin'`이 아니면 403
- **세션의 role 값을 신뢰하지 말고 매 요청 DB 조회할 것**

### 6-2. 라우트

| 라우트 | 메서드 | 기능 |
|---|---|---|
| `/admin` | GET | 대시보드: 신고 목록(대상/사유/시각/자동조치 여부), 자동조치 내역, 유저 목록, 상품 목록 |
| `/admin/user/<user_id>/suspend` | POST | 유저 정지 + `session_token=NULL` |
| `/admin/user/<user_id>/unsuspend` | POST | 정지 해제 + `report_count=0` 초기화 |
| `/admin/user/<user_id>/grant` | POST | 잔액 지급 (금액 입력, 양수 검증) — 송금 기능 시연용 |
| `/admin/product/<product_id>/block` | POST | 상품 차단 |
| `/admin/product/<product_id>/restore` | POST | 차단 해제 + `report_count=0` 초기화 |

- **전부 POST + CSRF 토큰.** GET으로 상태 변경 금지
- 모든 조치를 `admin_action_log`에 `actor_type='admin'`, `actor_id=<관리자 id>`로 기록
- 커밋 이후 `log_action()` 호출
- 관리자가 자기 자신을 정지시키는 것 방지
- `templates/admin.html` 신규 — `.admin-table`, `.badge`, `.badge-blocked`, `.badge-suspended`, `.btn-danger` 클래스 사용 (`base.html` 스타일 블록에 추가)
- `base.html` nav에 `role == 'admin'`일 때만 "관리자" 링크 노출 (**UI 숨김은 편의일 뿐, 실제 방어는 `admin_required`**)

### ✅ 6단계 자체 검증
- 관리자 로그인 → `/admin` 정상 표시, 신고/조치 내역 보임
- **일반 계정으로 `/admin` 직접 접근 → 403**
- 일반 계정으로 `/admin/user/<id>/suspend`에 POST 직접 전송 → 403
- CSRF 토큰 없이 관리자 조치 POST → 403
- 정지 → 해제 → 해당 유저 재로그인 가능 확인
- 차단 → 복구 → 상품 다시 조회 가능 확인
- 잔액 지급 후 송금 정상 동작
- `admin_action_log`에 `system`/`admin` 두 종류 행이 모두 존재

---

# 7단계. 세션 만료 검증 및 마무리

### 7-1. 세션 만료
`app.config['PERMANENT_SESSION_LIFETIME']`와 `app.permanent_session_lifetime`이 이미 30분으로 설정되어 있다. 로그인 시 `session.permanent = True`가 실제로 설정되는지 확인하고, 안 되어 있으면 추가할 것.

검증: `PERMANENT_SESSION_LIFETIME`을 임시로 5초로 낮춰 로그인 → 10초 대기 → 페이지 요청 시 로그인 페이지로 리다이렉트되는지 확인 → **확인 후 반드시 30분으로 원복**.

### 7-2. 전체 회귀 테스트
Phase 1·2A 기능이 살아있는지 전부 재확인:
회원가입 / 로그인 / 로그아웃 / 로그인 5회 실패 잠금 / CSRF 차단 / bio 수정 / 비밀번호 변경 및 재사용 방지 / 상품 등록·수정·삭제 / 이미지 업로드 / 상품 검색 / 공개 프로필 / rate limit 429 / 보안 헤더 6종 / audit_log 기록

### 7-3. 의존성 점검
`pip-audit`(없으면 `pip install pip-audit`) 실행 후 결과를 보고. 취약점이 있으면 목록과 조치 가능 여부를 함께 보고할 것. `requirements.txt`에 새로 추가한 패키지가 있으면 버전 고정.

### 7-4. 마이그레이션 재실행 안전성
서버를 2회 재기동해 `init_db()`가 반복 실행돼도 에러 없고 데이터(user/product/report/audit_log/global_message 행 수)가 유지되는지 확인.

---

## 최종 보고 형식

1. **단계별 자체 검증 결과** — 위 각 ✅ 항목을 실제 실행한 결과. 통과/실패를 항목 단위로
2. **변경 파일 목록 ↔ 이 지시서의 단계·항목 대응표**
3. **spec.md에서 의도적으로 벗어난 부분** (최소 1건: 1-2의 report 마이그레이션 가드) — 사유와 함께
4. **구현하면서 발견한 기존 코드의 문제** — 고쳤으면 어떻게 고쳤는지, 안 고쳤으면 왜인지
5. **불확실하거나 검증하지 못한 부분** — 추측으로 채우지 말고 "확인 못 함"이라고 명시할 것

---

## 하지 말 것

- Postgres 이주, Redis 도입, SQLAlchemy 도입 — 스코프 아님
- CSS 프레임워크 도입, 인라인 스크립트, `|safe`, 저장 전 이스케이프
- 기존 Phase 1·2A 라우트의 보안 로직 완화 (세션 쿠키 설정, rate limit, 검증 로직 등)
- `log_action()`을 트랜잭션 중간에서 호출
- 코드 생략 표기