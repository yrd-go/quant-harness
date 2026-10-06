from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 项目根目录（即 quant-harness 文件夹）
BASE_DIR = Path(__file__).resolve().parent

# 数据目录
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)  # 不存在则自动创建

# 输出目录
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 配置数据目录: 存放漏斗中间产物(candidates.json / target_pool.json 等)
# 注意: 这是目录, 与同目录下的 config.py 模块不冲突(模块优先于命名空间包)
CONFIG_DATA_DIR = BASE_DIR / "config"
CONFIG_DATA_DIR.mkdir(exist_ok=True)

# 日志目录
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# 统一的数据文件路径
DB_FILE = DATA_DIR / "quant_data.db"
DATA_LOG = DATA_DIR / "data_center.log"
FACTOR_LOG = DATA_DIR / "factor_score.log"
PRICE_PNG = OUTPUT_DIR / "price.png"
BACKTEST_PNG = OUTPUT_DIR / "backtest_result.png"

# --------------------------------------------------------------------------- #
# 时区: 全项目统一以【北京时间 UTC+8】为时间基准
#
# 为什么必须统一: 服务器/容器时区常常是 UTC, 直接 datetime.now() 会拿到
# 比北京时间早 8 小时的时间, 于是"今天"判断、增量更新的边界日期都可能错一天。
# 所有涉及"今天/当前"的逻辑都必须走 now_bj(), 不要直接用 datetime.now()。
# --------------------------------------------------------------------------- #
TIMEZONE = ZoneInfo("Asia/Shanghai")
TIMEZONE_NAME = "Asia/Shanghai (UTC+8)"


def now_bj() -> datetime:
    """当前北京时间(带时区信息的 datetime)。"""
    return datetime.now(TIMEZONE)


def today_bj_str(fmt: str = "%Y%m%d") -> str:
    """当前北京时间的日期字符串。

    默认格式 YYYYMMDD(无横线), 这是 akshare 大多数接口要求的日期格式。
    需要带横线时传 fmt="%Y-%m-%d"。
    """
    return now_bj().strftime(fmt)


# --------------------------------------------------------------------------- #
# 滚动数据窗口
# --------------------------------------------------------------------------- #
HISTORY_YEARS = 5              # 数据库只保留最近 N 年(滚动清理用)
START_DATE = "20210101"        # 初始建库的起始日期(YYYYMMDD, akshare 格式)
# END_DATE 不再写死成固定日期: None 表示"到当前北京时间为止"。
# 注意: src/data_center.py 里目前还有一个自己的 END_DATE 常量(第 2 步才会改它),
#       这里新增的 END_DATE 不会被它读取, 所以现在改动不会影响任何现有脚本。
END_DATE = None

# --------------------------------------------------------------------------- #
# akshare 请求限速与重试(集中管理, 避免散落在各个脚本里)
#
# 背景: 东方财富的接口已被实测限流(RemoteDisconnected)。限速的目的是
#       别把仅剩可用的接口也打挂, 所以宁可慢一点。
# --------------------------------------------------------------------------- #
AK_SLEEP_PER_REQUEST = 0.35    # 每次请求后的休眠(秒)
AK_SLEEP_EVERY = 30            # 每 N 次请求
AK_SLEEP_LONG = 2.0            # 每 N 次请求后额外休眠(秒)
AK_RETRY_TIMES = 3             # 单次请求失败重试次数
AK_RETRY_BACKOFF = 2.0         # 重试退避基数(秒): 第 1 次失败等 2s, 第 2 次等 4s

# 漏斗默认规模
SCREEN_AMOUNT_TOP_N = 300      # 按成交额保留前 N
SCREEN_MOMENTUM_TOP_N = 100    # 按动量保留前 N
SCREEN_MOMENTUM_WINDOW = 20    # 动量窗口(交易日)
