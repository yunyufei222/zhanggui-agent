# 掌柜智库 · 客服 Agent

面向在线教育场景的客服对话系统：**意图识别 → 补槽 → 查真值 → 作答或转人工**，
退款与工单能在多轮里办完，全过程可回放。核心特色是「宁可多问一句，也不答错一件」——
规则打底、LLM 仅作兜底、澄清优先于猜测、外部调用超时降级。

## 能力

- **对话**：规则意图识别（订单 / 政策 / 课程 / 进度 / 转人工），置信度不足才问模型；`mode=rule` 可完全离线跑。
- **四个技能**：订单查询（按单号取真值）、政策问答（知识库 + 兜底话术）、课程咨询（价格/班期/余位/已报名）、学习进度（中文数字归一、同名班次按日期让用户选）。
- **两类任务流**：退款申请 → 审批（单事务改退款/支付/订单/明细 + 报名关系）；工单提交 → 受理 → 完结 → 重开，人工回复播报给用户。
- **澄清四类**：多意图 / 模糊 / 缺参 / 冲突，`action=clarify` 可测。
- **可观测**：每次状态转换留痕（`idle→filling（clarify）`），`GET /sessions/{key}/trace` 可查。
- **调试台**：`GET /debug` 单页看对话、槽位、任务栈、轨迹，并能模拟付款/工单/退款。

## 快速开始

```bash
# 1) 起库（有 Docker 就用它）
docker compose -f docker/docker-compose.yaml up -d

# 2) 配置
cp .env.example .env          # 填数据库口令；不接模型可只留 DB_*

# 3) 装依赖 / 建表 / 灌数据
uv sync
uv run init_db.py
uv run -m generate.main --profile full
uv run init_kb.py             # 政策知识库 20 条（幂等）

# 4) 起服务（两个进程）
start-all.bat                 # main :8111 + agent :8112
```

打开调试台 <http://127.0.0.1:8112/debug>。

## 验收

```bash
.venv/Scripts/python.exe -m pytest -q       # 210 passed
.venv/Scripts/python.exe e2e.py              # 10/10 通过（真 HTTP，不用 TestClient）
```

`e2e.py` 会自己起两个服务，跑完整链：健康 → 下单 → 支付 → 报名 → 对话查单 → 政策命中 →
弱命中转人工建工单 → 工单流转 → 退款申请 → 审批后五处状态一致。

## 目录

```
app/       主应用：订单/支付/退款/工单/知识库的 HTTP 接口与规则层（services/）
agent/     客服 Agent：意图、澄清、任务栈、技能、会话存储、调试台与模拟接口
tests/     210 个用例（含 13 条契约回归、四类澄清、降级、配置、页面）
sql/       建表脚本        seeds/  基础种子数据      generate/  数据生成器
docker/    MySQL compose    demo.json  固定的演示对象（业务键，不写自增 id）
```

## 配置

- `agent/skills.yaml`：澄清阈值、工单 SLA、退款类型关键词、课程匹配正则、话术模板；启动时校验，写坏了直接报错。
- `.env`：数据库连接；`AGENT_LLM_ENABLED=1` + `AGENT_LLM_API_KEY` 才启用模型兜底（3s 超时、重试 1 次、失败标 `degraded`）。
- `demo.json`：演示学员/班期按业务键定位，换演示对象只改这一个文件。

## 已知未完

1. **多轮槽位只到 partial**：`orderNo` / `need` / `taskStack` 有，需求里的「学员身份」「班次」还没进槽位。
2. **课时级进度没有数据源**：进度只到班次（起止、剩余天数、报名状态），回复里如实说明。
3. **模型兜底默认关**：纯规则覆盖四类问句；配 key 即启用。
4. **测试会造数据**：`purge_demo.py` 可清（先 dry-run 看条数，`--yes` 才删）。

## 说明

本项目为课程实战项目，数据为生成数据（非真实用户信息）。
