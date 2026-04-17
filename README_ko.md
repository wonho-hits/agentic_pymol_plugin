# Agentic PyMOL Plugin

[English](./README.md) · **한국어**

PyMOL을 자연어로 조작하세요. PyMOL 콘솔에 요청을 입력하면 Gemini 기반의
DeepAgent가 계획을 세우고, 필요한 PyMOL Python 코드를 직접 작성·실행해
원하는 장면을 만들어 냅니다.

```text
PyMOL> ask 2wyk 리간드-단백질 interface를 stick으로 보여줘
[agent] ▶ 2wyk 리간드-단백질 interface를 stick으로 보여줘
[model] → run_pymol_python:
  cmd.fetch('2wyk', async_=0)
  cmd.select('lig', 'hetatm and not (resn HOH or solvent or inorganic)')
  cmd.show('sticks', 'byres polymer within 5 of lig')
  cmd.zoom('lig', 8)
[tool·run_pymol_python] OK
[agent] ✓ 2wyk를 로드하고 리간드 주변 5Å 잔기를 stick으로 표시했습니다.
```

---

## 아키텍처 한눈에 보기

이 플러그인은 **두 개의 분리된 Python 프로세스**로 돌아갑니다.

```
┌─────────────────────────────┐   ndjson over   ┌─────────────────────────────┐
│ PyMOL 프로세스 (Python 3.10)  │  stdin/stdout   │ Agent 프로세스 (Python 3.11)  │
│                             │ ◄─────────────► │                             │
│ • 플러그인 UI / 명령 등록       │   JSON 한 줄      │ • deepagents + LangChain    │
│ • 도구 핸들러 (exec, mutate,  │    = 메시지 1개    │ • Gemini (google-genai)     │
│   screenshot, ...)          │                 │ • Vision 분석 (로컬)          │
│ • AST safety 검사            │                 │ • uv가 관리하는 .venv          │
└─────────────────────────────┘                 └─────────────────────────────┘
```

왜 이렇게 나눴나요?
- PyMOL은 보통 Python 3.10을 쓰는데, 최신 LangChain / deepagents는 3.11+가
  편합니다.
- 의존성을 플러그인 쪽에 몰아넣으면 PyMOL의 파이썬 환경이 더러워지고
  충돌이 잦습니다.
- 에이전트 프로세스가 크래시해도 PyMOL은 멀쩡하게 살아 있습니다.

자세한 내부 구조는 [아키텍처](#아키텍처) 섹션을 참고하세요.

---

## 필요 조건

- **PyMOL** (Incentive 또는 Open-Source 모두 가능). Python 3.10 이상을 내장한
  최근 빌드를 권장합니다.
- **uv** — 에이전트용 Python 3.11 환경을 자동으로 설치·관리합니다.
  아직 없다면 한 줄로 설치됩니다.
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  설치 후 새 터미널을 열거나 `source ~/.zshrc` / `source ~/.bashrc`로
  PATH를 갱신하세요.
- **Gemini API 키** — <https://aistudio.google.com/app/apikey> 에서 발급받을
  수 있습니다.

---

## 처음부터 끝까지 따라하기

### 1. 소스 받기

원하는 위치에 저장소를 복제(또는 다운로드)합니다.

```bash
cd ~/Projects
git clone <your-fork-or-source> agentic_pymol_plugin
cd agentic_pymol_plugin
```

### 2. 에이전트 환경 설치 (uv)

에이전트 프로세스가 쓸 Python 3.11 가상환경을 만듭니다. 한 번만 하면 됩니다.

```bash
cd agent
uv sync
cd ..
```

처음 실행할 때 uv가 Python 3.11 인터프리터와 의존성(`deepagents`,
`langchain-google-genai`, `langgraph` 등)을 `agent/.venv/`에 설치합니다.

> **확인:** `ls agent/.venv/bin/python` 을 실행해 파일이 존재하면 성공입니다.

### 3. 플러그인 쪽 의존성 설치

플러그인은 PyMOL 내부에서 로드되므로, **PyMOL이 사용하는 Python**에
`python-dotenv` 하나만 설치하면 됩니다.

PyMOL이 쓰는 Python 경로를 모르면 PyMOL 콘솔에서:
```python
PyMOL> import sys; print(sys.executable)
```

그 경로로 설치:
```bash
/path/to/pymol/python -m pip install -r requirements.txt
```

Open-Source PyMOL을 conda로 쓰는 경우:
```bash
conda activate <your-pymol-env>
pip install -r requirements.txt
```

### 4. API 키 넣기

```bash
cp .env.example .env.local
```

에디터로 `.env.local`을 열어 `your_gemini_api_key_here` 자리를 실제 Gemini
키로 바꾸세요.

```dotenv
GOOGLE_API_KEY=여러분의_키
```

### 5. PyMOL에 플러그인 등록

PyMOL Plugin Manager는 `.py`, `.zip`, `.tar.gz`만 받기 때문에, 먼저
프로젝트 루트에서 zip을 빌드합니다.

```bash
make plugin
```

`dist/agentic_pymol_plugin.zip` 이 생성됩니다. 이 zip에는 PyMOL 안에서
실행되는 파일들(`__init__.py`, `config.py`, `plugin_side/`)만 들어있습니다.
`agent/` uv 프로젝트는 별도 프로세스로 실행되므로 zip에 포함되지 않습니다.

PyMOL 상단 메뉴에서:

**Plugin → Plugin Manager → Install New Plugin → Choose file...**

- `dist/agentic_pymol_plugin.zip` 을 선택하세요.
- PyMOL이 압축을 풀어 `~/.pymol/startup/agentic_pymol_plugin/` 로 설치합니다.
- zip에는 `.env.local` 과 `agent/` 프로젝트가 포함되지 않습니다. 설치 후
  연결해 주세요.
  ```bash
  cp .env.local ~/.pymol/startup/agentic_pymol_plugin/.env.local
  ```
  그리고 에이전트 프로젝트 위치를 다음 중 하나로 알려 주세요.
  - `.env.local` 에 절대 경로 추가:
    ```dotenv
    AGENTIC_PYMOL_AGENT_PYTHON=/절대/경로/agent/.venv/bin/python
    AGENTIC_PYMOL_AGENT_DIR=/절대/경로/agent
    ```
    `AGENTIC_PYMOL_AGENT_PYTHON` 만 지정해도 agent 디렉토리가 자동 추정
    됩니다. 레이아웃이 `agent/.venv/bin/python` 형태가 아니라면
    `AGENTIC_PYMOL_AGENT_DIR` 로 직접 지정하세요.
  - 또는 설치된 플러그인 폴더 옆에 agent 프로젝트를 심볼릭 링크:
    ```bash
    ln -s ~/Projects/agentic_pymol_plugin/agent \
          ~/.pymol/startup/agentic_pymol_plugin/agent
    ```

개발 중에는 zip을 매번 새로 빌드하지 말고, 소스 트리를 통째로 startup
폴더에 심볼릭 링크하는 [실시간 편집 작업 흐름](#실시간-편집-작업-흐름)을
권장합니다.

PyMOL을 재시작하면 플러그인이 자동으로 로드되면서 아래 명령들이 등록됩니다.

### 6. 첫 실행

PyMOL 콘솔에 입력:

```text
PyMOL> ask 1ubq 를 가져와서 카툰으로 표시해줘
```

처음 호출 시 에이전트 프로세스가 백그라운드로 기동하며
`[agent] ready (server v0.1.0)` 메시지가 뜹니다. 이후 요청은 이미 떠 있는
프로세스가 바로 처리하므로 빠릅니다.

---

## 사용법

PyMOL 콘솔에서 바로 입력하는 네 가지 명령을 제공합니다.

| 명령             | 설명                                                   |
| ---------------- | ------------------------------------------------------ |
| `ask <자연어>`   | 에이전트에게 요청을 보냅니다.                          |
| `ask_status`     | 현재 요청이 진행 중인지 보여줍니다.                    |
| `ask_cancel`     | 진행 중인 요청을 취소합니다.                           |
| `ask_reset`      | 대화 기록을 지웁니다(에이전트 프로세스를 재시작).      |

### 요청 예시

단순 로드 및 표시:
```text
ask 1crn을 가져와서 카툰으로 표시하고 hydrophobic 잔기를 주황색으로 강조해줘
```

상호작용 분석:
```text
ask 2wyk 리간드 주변 5Å 내 polar 잔기를 찾아서 잔기 이름과 거리를 알려줘
```

뷰 제어:
```text
ask 현재 selection을 중심으로 회전 애니메이션을 1초 동안 보여줘
```

### 진행 상황 읽기

에이전트가 일하는 동안 다음과 같은 형태의 로그가 PyMOL 콘솔에 흐릅니다.

```text
[agent] ▶ <여러분의 요청>
[model] → run_pymol_python:                              ← 에이전트가 도구 호출
  cmd.fetch('1ubq', async_=0)                            ← 여러 줄 코드는 들여쓰기
  cmd.show_as('cartoon', '1ubq')
[tool·run_pymol_python] OK                               ← 도구 결과 (짧으면 한 줄)
[agent] ✓ 1ubq를 가져와서 카툰으로 표시했습니다.         ← 최종 응답
```

복잡한 작업에서는 서브에이전트로 위임하기도 합니다:

```text
[model] → task(python_executor, "<하위 목표>")
```

중간에 멈추고 싶으면 `ask_cancel`, 완전히 처음부터 시작하고 싶으면
`ask_reset`을 입력하세요.

### 사용 가능한 도구

에이전트가 호출할 수 있는 도구 목록입니다. 대부분 ndjson 브릿지를 통해 PyMOL
쪽에서 실행되고, `describe_viewport`만 에이전트 프로세스에서 원격 스크린샷 +
로컬 Gemini Vision 호출을 체인합니다.

| 도구 | 실행 위치 | 설명 |
| ---- | --------- | ---- |
| `run_pymol_python(code)` | PyMOL | 라이브 세션에서 Python 코드 실행 |
| `inspect_session()` | PyMOL | 로드된 객체, 체인, 리간드, selection의 JSON 스냅샷 반환 |
| `mutate_residue(obj, chain, resi, target_aa)` | PyMOL | mutagenesis wizard를 안전하게 래핑한 단일 잔기 변이 |
| `capture_viewport()` | PyMOL | 현재 뷰포트 스크린샷을 임시 파일로 저장 |
| `describe_viewport()` | Agent (로컬) | 뷰포트를 캡처한 뒤 Gemini Vision으로 자연어 설명 반환 |

Main Agent는 위 도구 전부와 `task()` (서브에이전트 위임), `write_todos()` 를
사용합니다. Python Executor 서브에이전트는 `describe_viewport` 를 제외한
도구에 접근합니다.

---

## 설정

`.env.local`에서 아래 변수들을 조정할 수 있습니다.

| 변수                          | 기본값              | 설명                                                        |
| ----------------------------- | ------------------- | ----------------------------------------------------------- |
| `GOOGLE_API_KEY`              | *(필수)*            | Gemini API 키                                               |
| `GEMINI_API_KEY`              | —                   | `GOOGLE_API_KEY` 대체 가능                                  |
| `AGENTIC_PYMOL_MODEL`         | `gemini-3-flash-preview` | 사용 모델. preview가 rate-limit 걸리면 `gemini-2.5-flash`, 간단 작업엔 `gemini-2.5-flash-lite`, 복잡 작업엔 `gemini-2.5-pro` 사용 가능. |
| `AGENTIC_PYMOL_RECURSION`     | `50`                | LangGraph recursion 한도                                    |
| `AGENTIC_PYMOL_TIMEOUT`       | `60`                | 단일 tool 호출 timeout(초)                                  |
| `AGENTIC_PYMOL_HISTORY_TURNS` | `10`                | 유지할 최근 대화 턴 수. 오래된 턴은 토큰 절약을 위해 버려짐. |
| `AGENTIC_PYMOL_AGENT_PYTHON`  | *(자동탐지)*        | 에이전트가 쓸 Python 경로 수동 지정. 이 값으로부터 agent 프로젝트 루트를 추정합니다. |
| `AGENTIC_PYMOL_AGENT_DIR`     | *(자동추정)*        | `agent/` 프로젝트 루트 절대 경로. zip으로 설치한 경우 `agent/` 가 함께 복사되지 않으므로 필수. |

대부분의 경우는 `GOOGLE_API_KEY` 하나만 채우면 됩니다. 에이전트의 Python
경로는 `agent/.venv/bin/python`을 자동으로 사용합니다.

---

## 문제 해결

### `[agent] failed to start agent subprocess: agent python not found`

`agent/.venv`가 없거나 손상된 상태입니다. 프로젝트 루트에서:
```bash
cd agent && uv sync
```

### `[agent] config error: GOOGLE_API_KEY not set`

`.env.local`이 설치된 플러그인 폴더에 없거나 키 값이 비어 있습니다.
`~/.pymol/startup/agentic_pymol_plugin/.env.local` 을 확인하세요.

### `[agent] failed to start agent subprocess: ... No such file or directory: '.../agentic_pymol_plugin/agent'`

zip으로 설치한 경우 `agent/` 가 함께 복사되지 않아 발생하는 오류입니다.
`.env.local` 에 `AGENTIC_PYMOL_AGENT_PYTHON` (또는
`AGENTIC_PYMOL_AGENT_DIR`)을 지정하거나, 소스의 `agent/` 를 설치 폴더 옆에
심볼릭 링크하세요(5단계 참조).

### 에이전트 로그는 어디에?

에이전트 서브프로세스의 logging은 PyMOL 콘솔 대신 파일로 리다이렉트됩니다.
시작 시 경로가 표시됩니다:

```text
[agent] ready — model=... thread=... (stderr → .../agentic_pymol_plugin/agent.log)
```

디버깅 시 별도 터미널에서 실시간으로 확인할 수 있습니다:

```bash
tail -f ~/.pymol/startup/agentic_pymol_plugin/agent.log
```

세션마다 `--- agent-stderr session <id> ---` 헤더로 구분됩니다.

### 에이전트가 먹통이 되었을 때

```text
PyMOL> ask_cancel     # 현재 요청만 취소
PyMOL> ask_reset      # 프로세스 재시작 (대화 기록 초기화)
```

### 실시간 편집 작업 흐름

매번 zip을 다시 빌드해 플러그인 매니저로 재설치하는 대신, 소스 디렉토리를
PyMOL 시작 폴더로 심볼릭 링크하면 편합니다.

```bash
ln -s ~/Projects/agentic_pymol_plugin ~/.pymol/startup/agentic_pymol_plugin
```

원본을 수정하고 PyMOL을 재시작하면 즉시 반영됩니다.

---

## 아키텍처

```
ask "..."  ─►  AgentClient ─► subprocess (agent-server)
                    │                  │
                    │  ndjson request  │
                    ├─────────────────►│
                    │                  │  Main Agent (Gemini)
                    │                  │      │
                    │                  │      ├─ 간단  → run_pymol_python
                    │                  │      ├─ 조회  → inspect_session
                    │                  │      ├─ 변이  → mutate_residue
                    │                  │      ├─ 시각  → describe_viewport
                    │                  │      │          (로컬: capture + Gemini Vision)
                    │                  │      └─ 복잡  → task(python_executor)
                    │                  │
                    │◄─────────────────┤  tool_call (원격 도구)
                    │                  │
                    │  PyMOL 핸들러:    │
                    │  exec / snapshot │
                    │  mutate / png    │
                    │                  │
                    ├─────────────────►│
                    │  tool_result     │
                    │                  │
                    │◄─────────────────┤  event / done
                    ▼
              PyMOL 콘솔
```

### 구성 요소

- **`__init__.py`** — PyMOL 플러그인 엔트리. `ask`, `ask_status`,
  `ask_cancel`, `ask_reset`를 등록.
- **`plugin_side/agent_client.py`** — 에이전트 subprocess를 띄우고,
  백그라운드 스레드에서 ndjson을 읽어 이벤트를 콘솔에 렌더링하고,
  에이전트의 도구 호출을 적절한 핸들러에 디스패치.
- **`plugin_side/pymol_tools.py`** — PyMOL 안에서 실행되는 도구 핸들러:
  `run_pymol_python` (AST 검사 후 `exec()`), `inspect_session` (구조화
  JSON 스냅샷), `mutate_residue` (안전한 mutagenesis wizard 래퍼),
  `capture_viewport` (스크린샷을 임시 파일로 저장).
- **`plugin_side/safety.py`** — `os`, `subprocess`, `shutil`, `sys`,
  `socket`, `urllib`, `requests` 등 위험 import와
  `cmd.reinitialize()`, `cmd.delete('all')`, `cmd.quit()`,
  `open(..., 'w')` 등을 차단. `cmd.fetch` 등 정상 명령은 허용.
- **`agent/` (uv project)** — 실제 LLM 호출과 agent 루프. `deepagents`,
  `langchain-google-genai`, `langgraph`를 3.11 환경에 격리 설치. PyMOL 쪽에서
  import 하지 않습니다.
- **`agent/src/agent_server/__main__.py`** — ndjson 메시지 루프. `request`를
  `AgentRunner`에 넘기고, 대화 히스토리를 관리(설정 가능한 턴 수 cap)하며,
  이벤트와 도구 호출을 다시 stdout으로 내보냄.
- **`agent/src/agent_server/remote_tool.py`** — LangChain 도구 바인딩.
  원격 도구(`run_pymol_python`, `inspect_session`, `mutate_residue`)는
  ndjson `tool_call`을 보내고 `tool_result`가 올 때까지 차단.
  `describe_viewport`는 원격 스크린샷 캡처와 Gemini Vision API 호출을
  체인하는 로컬 도구.

### 메시지 프로토콜

모든 통신은 한 줄에 하나의 JSON 객체(ndjson)입니다.

- **Plugin → Agent:** `request`, `tool_result`, `cancel`, `shutdown`
- **Agent → Plugin:** `ready`, `event`, `tool_call`, `done`, `error`

스키마는 `agent/src/agent_server/protocol.py` 및 동일한 내용의 플러그인 쪽
복사본 `plugin_side/protocol.py`에 있습니다. 두 파일은
`tests/test_protocol_parity.py`가 서로 drift 하지 않도록 검증합니다.

---

## 개발자 가이드

### 설치용 zip 빌드

```bash
make plugin     # → dist/agentic_pymol_plugin.zip
make clean      # dist/ 삭제
```

Makefile은 플러그인 쪽 파일만 `dist/build/` 로 복사한 뒤 zip으로 묶고,
`__pycache__` 와 `.DS_Store` 는 제외합니다.

### 테스트 실행

플러그인 쪽(pymol 없이):
```bash
agent/.venv/bin/python -m pytest tests/ -q
```

에이전트 쪽:
```bash
cd agent && uv run pytest -q
```

### 에이전트만 단독 실행

디버깅 편의를 위해 PyMOL 없이 에이전트 프로세스만 띄울 수 있습니다.

```bash
cd agent
uv run agent-server
```

그런 다음 ndjson 메시지를 stdin으로 넣어 보세요(`Ctrl-D`로 종료):

```json
{"type":"request","id":1,"prompt":"hello"}
```

실제 PyMOL tool 호출은 플러그인이 있어야 처리되므로 이 모드에서는
`run_pymol_python` 호출이 timeout으로 실패합니다. 메시지 흐름 확인용입니다.

### 로그 레벨

```bash
export AGENTIC_PYMOL_LOG=DEBUG
```

에이전트 프로세스의 logging 레벨을 조절합니다(플러그인 옆의 `agent.log` 에
기록됩니다. 위의 "에이전트 로그는 어디에?" 참조).

---

## 안전에 관한 주의

이 플러그인은 **LLM이 작성한 임의 Python 코드를 여러분의 실행 중인 PyMOL
세션에서 실행**합니다. AST safety 계층이 명백한 위험 패턴을 차단하지만
완전한 샌드박스가 아닙니다. 다음을 지켜 주세요.

- 중요한 구조가 열려 있는 세션에서는 신중하게 사용하세요.
  작업 전 저장(`cmd.save('backup.pse')`)을 권장합니다.
- 예상 밖의 tool 호출이 보이면 `ask_cancel`로 멈추고 로그를 확인하세요.
- `.env.local`에는 API 키가 들어 있으므로 공개 저장소에 커밋되지 않도록
  `.gitignore`에 포함되어 있는지 확인하세요.

---

## 라이선스

(프로젝트 라이선스에 맞게 채워 넣으세요.)
