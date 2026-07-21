# Secure Coding

## Tiny Secondhand Shopping Platform.

You should add some functions and complete the security requirements.

## requirements

이 프로젝트는 두 가지 설치 방법 중 하나를 선택해서 사용할 수 있습니다 (둘 다 동일한 앱을 실행합니다).

### 방법 A. conda (enviroments.yaml)

```
git clone https://github.com/ugonfor/secure-coding
conda env create -f enviroments.yaml
```

### 방법 B. pip (requirements.txt)

```
git clone https://github.com/ugonfor/secure-coding
python -m venv venv
# Windows: venv\Scripts\activate / macOS·Linux: source venv/bin/activate
pip install -r requirements.txt
```

## 환경변수 설정 (.env)

이 프로젝트는 `SECRET_KEY` 등의 민감한 설정을 코드에 하드코딩하지 않고 `.env` 파일에서 읽어옵니다.

1. `.env.example`을 복사해서 `.env` 파일을 만듭니다.
   ```
   cp .env.example .env
   ```
2. `.env` 파일을 열어 아래 값을 채웁니다.
   - `SECRET_KEY`: 랜덤하게 생성한 긴 문자열 (예: `python -c "import secrets; print(secrets.token_hex(32))"`)
   - `FLASK_DEBUG`: 로컬 개발 중에만 `true`, 그 외에는 반드시 `false`
   - `ADMIN_USERNAME` / `ADMIN_PASSWORD`: 서버 최초 기동 시(`init_db()`) 해당 username의 계정이 없으면 `role='admin'`으로 자동 생성되는 관리자 테스트 계정입니다. 비밀번호는 bcrypt로 해싱되어 저장되며, 이미 계정이 존재하면 아무 동작도 하지 않습니다(멱등적). **관리자 전용 라우트/대시보드 등 관리자 기능 자체는 이번 Phase 1 범위가 아니며 Phase 2에서 구현될 예정입니다.** 관리자 테스트 계정으로는 `.env`의 `ADMIN_USERNAME`/`ADMIN_PASSWORD`로 일반 로그인 화면에서 로그인할 수 있습니다.
3. `.env` 파일이 없으면 앱이 `SECRET_KEY` 환경변수 누락으로 즉시 에러를 내며 실행되지 않습니다(조용히 기본값으로 넘어가지 않음).

### 파일 권한 (로컬/자체 서버 배포 시에만 해당)

`.env`와 `market.db`에는 비밀키, 비밀번호 해시 등 민감한 정보가 들어 있습니다. 자체 서버(리눅스 등)에 배포하는 경우, 소유자만 읽고 쓸 수 있도록 권한을 제한하세요.

```
chmod 600 .env
chmod 600 market.db
```

> **주의**: 이 항목은 로컬/자체 서버 배포에만 해당합니다. Render 같은 PaaS에 배포할 때는 `.env` 파일 자체를 올리지 않고 플랫폼의 환경변수 대시보드에 직접 입력하므로 해당 사항이 없습니다.

## usage

run the server process.

```
python app.py
```

if you want to test on external machine, you can utilize the ngrok to forwarding the url.
```
# optional
sudo snap install ngrok
ngrok http 5000
```

## Render 배포

이 프로젝트는 Render 무료 티어에 배포하는 것을 기준으로 안내합니다. GitHub 저장소를 Render에 연결하면 아래 설정만으로 자동 빌드/배포되며, HTTPS가 자동으로 적용됩니다.

- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 --bind 0.0.0.0:$PORT app:app`
  - `--bind 0.0.0.0:$PORT`를 반드시 포함해야 합니다. 포트 바인딩이 누락되면 서비스가 뜬 것처럼 보여도 실제로는 요청에 응답하지 않는 상태가 됩니다.
- **환경변수**: `.env` 파일을 업로드하지 말고, Render 대시보드의 Environment Variables에 아래 값을 직접 입력하세요.
  - `SECRET_KEY`
  - `FLASK_DEBUG=false`
  - `ADMIN_USERNAME`
  - `ADMIN_PASSWORD`

### 알아두어야 할 점 (캐비어트)

- **디스크 비영속성**: Render 무료 티어는 디스크가 비영속적(ephemeral)입니다. 재배포하거나 컨테이너가 재시작되면 `market.db`(사용자/상품/신고 데이터)와 `static/uploads/products/`(업로드 이미지, 추후 Phase에서 추가 예정)의 내용이 초기화될 수 있습니다. 이 프로젝트는 과제 데모/채점 목적이므로, 재배포를 자주 하지 않는 선에서 이 리스크를 감수하기로 결정했습니다. (Postgres 등 외부 영속 스토리지로 옮기는 작업은 하지 않습니다.)
- **콜드 스타트**: 무료 티어는 15분간 요청이 없으면 슬립 상태로 전환됩니다. 슬립 이후 첫 요청은 서버가 깨어나는 데 최대 약 60초 정도 걸릴 수 있습니다.
