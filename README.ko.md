# sparring — Worker × Reviewer 교차 검토 루프

[English](README.md) | **한국어**

> 영어판([README.md](README.md))이 기준입니다. 이 문서는 반영이 늦을 수 있습니다.

> **공지 (2026-11-17)** npm 배포 없이 GitHub에서 clone 받아 설치하는 방식으로만 공유합니다. 아래 [한 줄 설치](#한-줄-설치)를 따라 하세요.

## 요약

sparring은 Claude Code 스킬(과 CLI)입니다. 환경에 맞게 Node나 Python(uv)으로 [한 줄 설치](#한-줄-설치)를 하고, 작업물을 검증하거나 구현할 때 쓰면 됩니다. Claude Code에 평소 말투로 이렇게 말하세요:

> “ㅇㅋ그 설계안 토대로 구현 스파링해줘. 니가 워커 하고 코덱스가 리뷰어로 해서 1라운드만해. 와리가리 귀찮으니까 워크트리로 하지말고 그냥 이 폴더에서 스파링해”
>
> → `--worker claude --reviewer codex --rounds 1 --in-place`

> “방금 니가 작업한 문서 스파링해. 코덱스가 리뷰하게 해. 내용이 복잡하니까 2라운드 스파링떠”
>
> → `--worker claude --reviewer codex --rounds 2`

> “이것좀 구현해봐. 니가 리뷰어, 니가 워커로 스파링 1라운드해”
>
> → `--worker claude --reviewer claude --rounds 1`

한 에이전트가 작업하고(Worker), 다른 에이전트가 검토하고(Reviewer), 다시 Worker가 반영하는 과정을 CLI 한 줄로 돌리는 도구입니다.

- 로그인해 둔 `codex`, `claude` CLI를 그대로 실행합니다. API 키를 따로 쓰지 않습니다.
- 기본 조합은 Codex 작업, Claude 리뷰입니다. 반대로 바꾸거나 같은 에이전트끼리 짝지을 수도 있습니다.
- 코드 구현뿐 아니라 설계, 조사, 문서 작성, 보안 검토처럼 결과물이 있는 작업이면 같은 흐름으로 처리합니다.
- 작업은 별도 git worktree/브랜치(`sparring/<task-id>`)에서 진행되므로 원본 작업 트리는 건드리지 않습니다. 결과가 마음에 들면 `git merge` 하면 됩니다.

## 주의

- 에이전트는 작업 저장소에 **쓰기 권한**과 **네트워크 접근**을 가진 채 실행됩니다(Worker 기준. Reviewer는 읽기 전용). 기본은 별도 git worktree에서 작업하지만, `--in-place`를 쓰면 현재 작업 트리를 직접 수정합니다. 결과는 병합 전에 직접 검토하세요.
- `codex`, `claude` CLI를 그대로 실행하므로 **각 CLI의 구독 사용량(또는 API 비용)**을 씁니다. 라운드 수가 늘수록 사용량도 늘어납니다.
- 에이전트에게는 허용 목록의 환경변수와 저장소의 `.env.agent`만 전달됩니다. 비밀 값은 `.env.agent`에 넣지 마세요. 에이전트가 읽을 수 있습니다.

## 설치

### 준비물

- `git`
- [`codex`](https://github.com/openai/codex) CLI, [`claude`](https://docs.claude.com/en/docs/claude-code) CLI를 설치하고 각각 로그인
- [`uv`](https://docs.astral.sh/uv/) 또는 Node.js 18.17 이상 (둘 중 하나). uv를 쓰면 Python이 없어도 uv가 알아서 받습니다.

### 한 줄 설치

저장소를 `~/sparring`에 받고, `sparring` CLI와 Claude Code 스킬을 함께 설치합니다. Node 버전과 Python 버전은 기능이 같고 기록 파일(`STATE.json`, `~/.sparring/`)도 공유합니다. 하나만 설치하세요.

```bash
# uv (Python)
git clone https://github.com/Potatoplz/sparring ~/sparring && uv tool install -e ~/sparring/python && mkdir -p ~/.claude/skills && ln -sfn ~/sparring/plugins/sparring/skills/sparring ~/.claude/skills/sparring

# Node
git clone https://github.com/Potatoplz/sparring ~/sparring && (cd ~/sparring/node && npm install && npm run build && npm install -g .) && mkdir -p ~/.claude/skills && ln -sfn ~/sparring/plugins/sparring/skills/sparring ~/.claude/skills/sparring

sparring --version
```

uv가 없으면 먼저 `curl -LsSf https://astral.sh/uv/install.sh | sh`로 설치합니다.

### 업데이트

```bash
cd ~/sparring && git pull                  # uv: 이것으로 끝
cd ~/sparring/node && npm run build        # Node만: 다시 빌드
```

## Claude Code에서 이렇게 말해 보세요

스킬을 설치했다면 Claude Code에서 평소처럼 말하면 됩니다. Claude가 말에서 옵션을 골라 `sparring`을 실행하고, 끝나면 결과를 보고합니다.

| 이렇게 말하면 | 이렇게 실행됩니다 |
|---|---|
| "로그인 API에 rate limit 추가하는 거 스파링해줘" | Codex 작업 → Claude 리뷰, 1라운드 (기본) |
| "README 설치 섹션 정리 스파링해줘. 네가 워커, 코덱스가 리뷰어" | `--worker claude --reviewer codex` |
| "결제 모듈 리팩터링 스파링 붙여줘. 3번 주고받아" | `--rounds 3` |
| "장바구니 수량 버그 스파링으로 고쳐줘. 테스트 돌려서 확인하고" | `--test "<프로젝트 테스트 명령>"` |
| "인증 흐름 보안 검토 스파링해줘. 코덱스끼리" | `--worker codex --reviewer codex` |
| "변경 이력 문서 초안 스파링으로 써줘. 리뷰 없이" | `--rounds 0` |
| "아까 스파링 이어서 테스트 코드도 추가해줘" | `--continue <task-id>` |
| "스파링 어디까지 됐어?" | `sparring ls`/로그 확인 후 요약 |
| "스파링 결과 머지해줘" | 결과 확인 후 `git merge sparring/<task-id>` |

"스파링"이라는 말이 들어가야 스킬이 동작합니다. 일반 요청에는 끼어들지 않습니다.

## 언어

로그, 에이전트 보고, TASK.md/REVIEW.md, 오류 메시지는 한국어(`ko`)와 영어(`en`)를 지원합니다.

- 실행: `--lang ko|en` → 환경변수 `SPARRING_LANG` → 로케일(`LC_ALL`, `LC_MESSAGES`, `LANG`) 순으로 정합니다. 재개하면 처음 실행한 언어를 유지합니다.
- 보기: `sparring watch`/`ls`는 보는 사람의 언어로 보여줍니다. 영어로 실행한 작업도 `sparring watch --lang ko`로 한국어로 볼 수 있습니다.
- macOS 터미널은 `LANG=en_US.UTF-8`인 경우가 많습니다. 한국어로 고정하려면 셸 설정에 `export SPARRING_LANG=ko`를 넣으세요.

터미널에서 볼 때는 화면 폭에 맞춰 줄을 바꿉니다. `sparring.log` 파일과 파이프 출력은 줄을 바꾸지 않습니다.

## 한 라운드의 흐름

1. **Worker 작업**: 저장소를 수정한 뒤 요약과 변경 파일 목록을 보고합니다.
2. **Validation**: `--test`를 지정했다면 sparring이 직접 실행합니다. 실패하면 Reviewer가 PASS를 줘도 NEEDS_REVISION으로 바뀝니다.
3. **Reviewer 검토**: 읽기 전용으로 실행되며 diff와 Worker 보고를 보고 `PASS` / `NEEDS_REVISION` / `HUMAN_REVIEW`로 판정합니다. `HUMAN_REVIEW`이면 멈추고, `--resume --note`로 판단을 전달하면 이어서 진행합니다.
4. Worker는 타당하지 않다고 본 지적을 근거와 함께 반려할 수 있고, 다음 라운드의 Reviewer는 이전 지적마다 해결 / 미해결 / 반려 인정 중 하나로 판정합니다.
5. `--rounds N`번 주고받습니다(기본 1, 0이면 Worker만 실행). PASS가 나와도 지정한 라운드는 모두 진행합니다.

마지막 라운드의 수정은 다시 리뷰받지 않습니다. 그래서 종료 요약의 "최종 리뷰"는 마지막 수정 **전**의 판정입니다. NEEDS_REVISION으로 끝났다면 그 지적을 Worker가 어떻게 반영하거나 반려했는지 로그에서 확인하세요.

## 안전장치

| 항목 | 내용 |
|------|------|
| 격리 | 기본은 `git worktree`에서 HEAD 기준으로 작업하므로 `.env` 같은 untracked 파일은 에이전트가 볼 수 없습니다. |
| 권한 | Worker는 쓰기 가능, Reviewer는 읽기 전용입니다(codex는 `read-only` sandbox, claude는 plan 모드와 Edit/Write 차단). |
| 환경변수 | 허용 목록(PATH, HOME, LANG 등)에 있는 변수만 에이전트에 넘깁니다. 추가로 필요한 값은 저장소의 `.env.agent`에 적습니다. |
| 한도 감지 | 구독 사용량 한도에 걸리면 `limit` 상태로 멈추고 작업을 보존합니다. 한도가 풀린 뒤 `--resume`으로 이어갑니다. |
| 세션 유지 | Reviewer는 라운드가 바뀌어도 같은 CLI 세션을 씁니다(`--fresh-reviewer`로 매번 새로). |

## 명령어

```bash
sparring "로그인 API에 rate limit 추가"                     # codex 작업 → claude 리뷰 → 수정 1회
sparring --worker claude "README 설치 섹션 정리"             # 역할 바꾸기
sparring --worker codex --reviewer codex "CLI 옵션 파서 정리" # 같은 에이전트끼리 (세션 분리)
sparring --rounds 3 "결제 모듈 리팩터링"                     # 3번 주고받기
sparring --rounds 0 "변경 이력 문서 초안 작성"                # Worker만
sparring --test "npm test" "장바구니 수량 버그 수정"          # 매 라운드 검증
sparring --title "인증 보안 검토" "..."                      # sparring ls에 보일 제목
sparring --timeout 3600 "대규모 마이그레이션 설계"            # 에이전트 1턴 제한(초, 기본 1800)
sparring --repo ~/work/my-app "헬스체크 엔드포인트 추가"       # 다른 저장소
sparring --in-place "오타 수정"                             # worktree 없이 현재 트리에서
sparring --json "의존성 버전 점검"                           # JSON 요약 출력

sparring --lang en "..."                                   # 영어로 실행 (로그와 에이전트 보고)

sparring ls                 # task 목록 (이 저장소 / -a 전체)
sparring watch [n|id 일부]   # 진행 로그 실시간 보기
sparring watch --brief      # 리뷰 판정과 종료 요약만 (--verbose: 전부, --all: 처음부터)
sparring watch --lang en    # 다른 언어로 보기

sparring --resume <task-id>                         # 중단된 task 재개
sparring --resume <task-id> --note "v2 스펙 기준"     # HUMAN_REVIEW에 답하고 재개
sparring --continue <task-id> "테스트 코드도 추가"     # 이전 task의 브랜치/세션 이어서
sparring --continue <task-id> --sync "..."          # 이어가기 전에 현재 HEAD 병합

git merge sparring/<task-id>     # 결과 반영
sparring --cleanup <task-id>     # worktree 정리 (브랜치와 기록은 유지)
```

## 정리

`sparring --cleanup`은 worktree만 지웁니다. 결과를 다시 보거나 병합할 수 있도록 브랜치와 기록은 남깁니다. task를 완전히 지우려면:

```bash
sparring --cleanup <task-id>         # worktree 삭제 (~/.sparring/worktrees/<task-id>)
git branch -D sparring/<task-id>     # 브랜치 삭제 (병합한 뒤, 또는 결과가 필요 없을 때)
rm -rf .agents/<task-id>             # 작업 기록 삭제
```

- `.agents/`는 저장소의 `.git/info/exclude`에 자동으로 등록되므로 `git status`나 커밋에 섞이지 않습니다. `.gitignore`는 건드리지 않습니다.
- `--cleanup`, `--resume`, `--continue`는 어느 폴더에서 실행해도 됩니다. `sparring watch`처럼 task 목록에서 task id로 저장소를 찾습니다. 직접 지정하려면 `--repo <경로>`를 붙이세요.
- task가 아직 실행 중이거나 worktree에 커밋하지 않은 변경이 있으면 정리하지 않습니다.
- task 목록(`~/.sparring/tasks.jsonl`)에는 `sparring ls -a`용으로 task마다 한 줄이 남습니다. 지우지 않아도 됩니다.

## 작업 기록 (`<repo>/.agents/<task-id>/`)

```text
TASK.md            요청 내용 + 사용자 판단(--note)
REVIEW.md          최신 리뷰 (REVIEW-r1.md, REVIEW-r2.md …)
r1.diff            누적 변경분 / r2-inc.diff 이전 리뷰 이후 수정분
final.diff         최종 변경분
STATE.json         현재 단계, 판정, 토큰/비용
events.jsonl       진행 기록 (이벤트당 JSON 한 줄, sparring watch는 이것을 읽어 보여줌)
sparring.log       events.jsonl을 실행 언어로 옮긴 진행 로그 (tail/cat용)
logs/              에이전트별 원본 출력
```

전체 task 목록은 `~/.sparring/tasks.jsonl`, worktree는 `~/.sparring/worktrees/`에 있습니다(`SPARRING_HOME`, `SPARRING_WORKTREES`로 변경).

## 종료 코드

| 코드 | 의미 |
|------|------|
| 0 | COMPLETED (모든 라운드 완료, 마지막 판정은 로그에) |
| 1 | 에이전트 오류 (`--resume`으로 재시도) |
| 2 | 실행 전 오류 (git 저장소 아님, CLI 없음, `--test` 명령 없음 등) |
| 3 | HUMAN_REVIEW (`--resume --note`로 답변) |
| 4 | 최종 Validation 실패 |
| 5 | 사용량 한도 (한도가 풀린 뒤 `--resume`) |
| 130 | 사용자 중단 |

## 라이선스

MIT. [LICENSE](LICENSE)를 참고하세요.

## 개발

```text
python/    Python 구현 (pyproject.toml, src/sparring/cli.py)
node/      Node 구현 (package.json, src/cli.ts)
shared/    두 구현이 같이 쓰는 파일: messages.json (ko/en 문장과 에이전트 프롬프트)
plugins/   Claude Code 플러그인 (스킬), .claude-plugin/ 은 마켓플레이스 정의
scripts/   gen_messages.py: shared/messages.json 생성 스크립트
tests/     두 구현 비교 테스트 (tests/run.sh)
```

- 동작은 두 구현이 똑같아야 합니다. 설치할 때 `shared/messages.json`이 각 패키지 안으로 복사됩니다.
- 문장이나 프롬프트를 고칠 때는 `scripts/gen_messages.py`를 수정하고 `python3 scripts/gen_messages.py`로 다시 생성하세요.
- 한쪽을 고치면 다른 쪽도 고치고 `tests/run.sh`를 실행하세요. 가짜 `codex`/`claude`로 같은 시나리오(기본 흐름, 2라운드, HUMAN_REVIEW, 한도, 오류, 타임아웃, 중단, `--continue`, `--cleanup`, 실행 전 오류 등)를 두 구현에 돌리고, 남긴 기록·로그·에이전트에 넘긴 인자·종료 코드가 같은지 비교합니다. 실제 `~/.sparring`은 건드리지 않습니다.
