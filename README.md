# 实时股票行情服务器 (Realtime Stock Quote Server)

纯爬虫、零依赖、无需注册/付费的实时股价 HTTP 服务，覆盖**美股 / 港股 / A股**，
美股支持**盘前(PRE) / 盘中(REGULAR) / 盘后(POST) / 夜盘(OVERNIGHT)**。
支持**模糊搜索**（名称/拼音/英文/代码）并自动返回**主要上市地**。

## 三大能力

1. **模糊搜索（像券商 App 一样）**
   `腾讯` / `tencent` / `700` / `0700.HK` 都能查到腾讯。
   多地上市的公司自动返回**主要上市地**：`阿里巴巴` → 港股 09988（而不是美股 ADR BABA），
   `京东`→09618、`网易`→09999、`小米`→01810，`苹果`→US AAPL，`茅台`→沪 600519。
   - 中文/拼音走腾讯 smartbox；英文走 Yahoo search（用 `longname` 把同一家公司的多个市场归并）。
   - 主市场优先级 **HK > CN > US**（美股里的中概股多为 ADR，属次要上市）。
   - 结果缓存 10 分钟，重复查询毫秒级返回。

2. **快（一个源返回就走）**
   每个市场的多个数据源**并发竞速**，谁先返回用谁，不做多源交叉验证。
   港股/A股稳定在 **~80ms**；美股 **~0.6s**（Yahoo+Robinhood 并发合并，自动选择当前时段）。
   美股只用 Yahoo + Robinhood：Yahoo 出 `marketState` / 盘前 / 盘后，Robinhood 补夜盘，
   Yahoo 不可用时退到 Robinhood 单源；其余源在盘前/盘后/夜盘滞后，故对美股禁用。

3. **抗频控（markdown.new / r.jina.ai 兜底）**
   直连优先（最快）。一旦某源被限频/封 IP（HTTP 429/403 或连接被重置），
   自动改走阅读代理 `markdown.new` / `r.jina.ai`（服务端换 IP 代抓）绕过风控。
   > 说明：代理单次延迟明显高于直连（~3-4s），所以只在"被频控时"按需启用，而不是全程使用——
   > 这样既"用得上 markdown.new 抗频控"，又不拖慢正常请求。腾讯/新浪几乎不限频，默认直连。

## 数据源

| 源 | 美股 | 港股 | A股 | 用途 |
|----|----|----|----|----|
| Yahoo (v7 quote + v8 chart) | ✓ | ✓ | ✓ | 美股 marketState / 盘前 / 盘后（主）；港股/A股备用 |
| Robinhood (公共 /quotes/) | ✓ | | | 美股夜盘 / 24h 成交价；Yahoo 不可用时兜底 |
| Tencent `qt.gtimg.cn` | | ✓ | ✓ | 港股 / A股主源，最快最稳 |
| Sina `hq.sinajs.cn` | | ✓ | ✓ | 港股 / A股备用 |
| Eastmoney `push2.eastmoney.com` | | ✓ | ✓ | 港股 / A股备用（被封时走代理绕过） |
| Tencent smartbox / Yahoo search | — | — | — | 模糊搜索 / 主上市地判定 |

> **美股只用 Yahoo + Robinhood。** 腾讯 / 新浪 / 东财在美股盘前、盘后、夜盘的数据
> 滞后严重，已从美股竞速与 `source=` 强制中禁用（对美股强制这些源会返回错误）。

## 启动

```bash
python3 stock_server.py                    # 默认 0.0.0.0:8849
python3 stock_server.py --port 9000        # 或指定端口
```

> 纯标准库、零依赖，Python 3.8+ 直接运行。所有配置均可用环境变量覆盖（见下表），便于容器化。

### 配置（环境变量）

| 变量 | 默认 | 说明 |
|----|----|----|
| `HOST` | `0.0.0.0` | 监听地址（亦可 `--host`） |
| `PORT` | `8849` | 监听端口（亦可 `--port`） |
| `HTTP_TIMEOUT` | `12` | 单个上游数据源抓取超时（秒） |
| `QUOTE_TIMEOUT` | `8` | 跨数据源竞速取价的总预算（秒） |
| `RESOLVE_CACHE_TTL` | `600` | 模糊搜索/解析结果缓存 TTL（秒） |
| `RESOLVE_CACHE_MAX` | `1024` | 解析缓存最大条目数（LRU 淘汰，防内存增长） |
| `SOCKET_TIMEOUT` | `20` | 客户端 socket 读超时（秒，防 slowloris 慢速攻击） |
| `MAX_RESPONSE_BYTES` | `5000000` | 单个上游响应大小上限（防 OOM / 解压炸弹） |
| `RATE_LIMIT_RPM` | `120` | 每客户端每分钟对 `/quote`、`/search` 的请求上限（`0` 关闭） |
| `TRUST_PROXY` | `0` | 置 `1` 时信任 `X-Real-IP` / `X-Forwarded-For`（仅在反代后面开启） |
| `AUTH_TOKEN` | （空） | 设置后，除 `/health` 外所有请求都需带 `Authorization: Bearer <token>`；逗号分隔可配多个便于轮换；留空则关闭鉴权 |

## 容器化部署 (Docker)

镜像由 GitHub Actions 自动构建并推送到 GHCR（多架构 amd64 / arm64）。

```bash
# 直接拉取运行（master 分支对应 latest）
docker run -d --name stockpricer -p 8849:8849 ghcr.io/soulbasic/stockpricer:latest

# 或本地构建
docker build -t stockpricer .
docker run -d -p 8849:8849 stockpricer

# 或用 compose（推荐：变量集中在 docker-compose.yml / .env）
cp .env.example .env        # 可选：按需修改变量
docker compose up -d
```

容器内置 `HEALTHCHECK`（探测 `/health`）。所有上表变量均可通过 `-e VAR=值` 或 compose 的 `environment` 注入，例如改端口：`-e PORT=9000 -p 9000:9000`。验证：

```bash
curl 'http://127.0.0.1:8849/health'
curl 'http://127.0.0.1:8849/quote?q=腾讯'
```

镜像标签：`latest`（默认分支）、`master`、`sha-<short>`，以及打 `vX.Y.Z` tag 时的 `X.Y.Z` / `X.Y`。

## 公网部署与安全

服务本身是明文 HTTP，**不要把 8849 直接暴露公网**，应放在 TLS 反向代理后面：

```
client ──HTTPS──> 反代 (Caddy/nginx) ──HTTP──> 127.0.0.1:8849 (stockpricer)
```

- 鉴权（可选）：设 `AUTH_TOKEN` 后，除 `/health` 外所有请求都必须带 `Authorization: Bearer <token>`，否则返回 `401`。令牌用常数时间比较；逗号分隔可同时配多个，便于无缝轮换。健康探针走 `/health`，不受鉴权影响。

  ```bash
  AUTH_TOKEN=$(openssl rand -hex 32) docker compose up -d   # 生成并启用
  curl -H "Authorization: Bearer <token>" 'http://127.0.0.1:8849/quote?q=腾讯'
  ```
- 内置防护：每客户端令牌桶限流（`RATE_LIMIT_RPM`）、socket 读超时防慢速攻击（`SOCKET_TIMEOUT`）、上游响应大小封顶防解压炸弹（`MAX_RESPONSE_BYTES`）、解析缓存 LRU 上限（`RESOLVE_CACHE_MAX`）。
- 容器加固：非 root、只读根文件系统、`cap_drop: ALL`、`no-new-privileges`、内存/进程数上限（见 `docker-compose.yml`）。
- compose 默认把端口绑在 `127.0.0.1`（`BIND_ADDR`），仅供同机反代访问；反代后请设 `TRUST_PROXY=1` 以便按真实客户端 IP 限流。

现成反代配置见 [`deploy/`](deploy/)：`Caddyfile`（自动 HTTPS，最省事）、`nginx.conf`（含 `limit_req` 限流与超时）、以及 `deploy/README.md`。

## 接口

| 路径 | 说明 |
|----|----|
| `GET /quote?q=腾讯` | 模糊查询（名称/拼音/代码），自动市场 + 主上市地 |
| `GET /quote?q=tencent` / `?q=700` | 英文 / 纯代码 |
| `GET /quote?symbol=AAPL` | 精确代码（大写视为 ticker，直连最快） |
| `GET /quote?q=阿里巴巴` | 多地上市 → 港股 09988（主） |
| `GET /quote?q=AAPL&market=us` | 强制市场 |
| `GET /quote?q=AAPL&source=robinhood` | 强制单一数据源 |
| `GET /quote?q=AAPL&fuzzy=0` | 关闭模糊解析 |
| `GET /quote?q=AAPL&raw=1` | 附带上游原始返回 |
| `GET /search?q=腾讯` | 只做解析：返回命中代码 + 其它上市地(alternatives) |
| `GET /health` / `GET /` | 健康检查 / 用法 |

## 返回示例（美股盘前）

```json
{
  "ok": true,
  "symbol": "AAPL",
  "name": "Apple Inc.",
  "market": "US",
  "currency": "USD",
  "marketState": "PRE",
  "price": 213.46,
  "previousClose": 211.18,
  "change": 2.28,
  "changePercent": 1.0796,
  "timestamp": "2026-07-14 20:25:10",
  "volume": 128340,
  "amount": null,
  "markdown": "### Apple Inc. `AAPL` · US · 盘前\n\n# 213.46 USD\n\n<font color=\"info\">▲ +2.28 (+1.08%)</font>\n\n> 上个收盘价 211.18 · 价格时间 2026-07-14 20:25:10（北京时间）\n\n**成交量** 128.34K"
}
```

`marketState`: `PRE` / `REGULAR` / `POST` / `OVERNIGHT` / `CLOSED`。

接口只返回当前有效时段的行情，不再展开其它时段。`previousClose` 始终是当前时段的
最近一次常规盘收盘价：盘前/盘中对应上个交易日收盘价，盘后/夜盘对应当天常规盘收盘价；
`change` 和 `changePercent` 均以它为基准。扩展时段上游没有成交量/额时返回 `null`，
不会用盘中累计值代替。

`markdown` 仅使用企业微信支持的标题、加粗、行内代码、引用及字体颜色语法；
上涨为 `info`（绿色 ▲），下跌为 `warning`（橙红色 ▼），平盘为 `comment`（灰色 —）。

## 自测

```bash
./selftest.sh             # 22 项：状态码 + 模糊解析主上市地 + 各市场报价（默认 8849）
python3 q.py 腾讯 阿里巴巴 茅台 苹果 700 AAPL   # 命令行速查
```
