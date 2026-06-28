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
   港股/A股稳定在 **~80ms**；美股 **~0.6s**（Yahoo+Robinhood 并发合并，含时段拆分）。
   美股给 Yahoo 一个 0.8s 的"优先窗口"以拿到带 `marketState` 的丰富数据，超时则回退到已就绪的快源。

3. **抗频控（markdown.new / r.jina.ai 兜底）**
   直连优先（最快）。一旦某源被限频/封 IP（HTTP 429/403 或连接被重置），
   自动改走阅读代理 `markdown.new` / `r.jina.ai`（服务端换 IP 代抓）绕过风控。
   > 说明：代理单次延迟明显高于直连（~3-4s），所以只在"被频控时"按需启用，而不是全程使用——
   > 这样既"用得上 markdown.new 抗频控"，又不拖慢正常请求。腾讯/新浪几乎不限频，默认直连。

## 数据源

| 源 | 美股 | 港股 | A股 | 用途 |
|----|----|----|----|----|
| Yahoo (v7 quote + v8 chart) | ✓ | ✓ | ✓ | 美股 marketState / 盘前 / 盘后（主） |
| Robinhood (公共 /quotes/) | ✓ | | | 美股夜盘 / 24h 成交价 |
| Tencent `qt.gtimg.cn` | ✓ | ✓ | ✓ | 港股 / A股主源，最快最稳 |
| Sina `hq.sinajs.cn` | ✓ | ✓ | ✓ | 备用，美股带盘后字段 |
| Eastmoney `push2.eastmoney.com` | ✓ | ✓ | ✓ | 备用（被封时走代理绕过） |
| Tencent smartbox / Yahoo search | — | — | — | 模糊搜索 / 主上市地判定 |

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
| `US_PREFER_GRACE` | `2.5` | 为更丰富的美股源（盘前/盘后/夜盘）多等待的宽限（秒） |
| `RESOLVE_CACHE_TTL` | `600` | 模糊搜索/解析结果缓存 TTL（秒） |

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

## 返回示例（美股，盘后/夜盘时段）

```json
{
  "ok": true, "query": "苹果", "market": "US", "symbol": "AAPL", "code": "AAPL",
  "name": "Apple Inc.", "currency": "USD", "price": 295.64,
  "previousClose": 298.01, "change": -2.37, "changePercent": -0.80,
  "marketState": "OVERNIGHT",
  "session": {
    "regular":   {"price": 297.01, "changePercent": -0.34},
    "pre":       null,
    "post":      {"price": 295.65, "changePercent": -0.46},
    "overnight": {"price": 295.64, "time": "2026-06-22T23:59:33Z"}
  },
  "resolved": {"market": "us", "code": "AAPL", "name": "Apple Inc.", "alternatives": []},
  "source": "yahoo+robinhood", "latencyMs": 612
}
```

`marketState`: `PRE` / `REGULAR` / `POST` / `OVERNIGHT` / `CLOSED`。

## 自测

```bash
./selftest.sh             # 22 项：状态码 + 模糊解析主上市地 + 各市场报价（默认 8849）
python3 q.py 腾讯 阿里巴巴 茅台 苹果 700 AAPL   # 命令行速查
```
