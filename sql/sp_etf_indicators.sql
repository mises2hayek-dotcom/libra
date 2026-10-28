-- ============================================================================
-- sp_etf_indicators —— 在库内重算 ETF 指标，Python 侧只负责入库原始行情
--
-- 参数
--   p_symbol   ETF 代码；NULL 或 '' 表示全部标的
--   p_adjust   复权类型 none/qfq/hfq；NULL 或 '' 表示全部
--   p_fill_pct 1 = 用相邻交易日收盘补齐缺失的 pct_change / price_change，0 = 不补
--              （amplitude 不受此开关影响：任何源都没有稳定的振幅字段，见下）
--
-- 口径（与 etf_daily.py 的 add_moving_averages / add_bollinger / add_band_metrics 一致）
--   ma5/ma20/ma60 = 最近 N 个交易日收盘均值，不足 N 日返回 NULL（对齐 pandas min_periods=N）
--   bb_mid        = ma20
--   bb_upper      = ma20 + 2 * STDDEV_POP(close, 20)      -- 总体标准差 ddof=0
--   bb_lower      = ma20 - 2 * STDDEV_POP(close, 20)
--   dist_to_lower = close - bb_lower                      -- 用已取整到 0.001 的 bb_lower
--   band_pos      = (close - bb_lower) / (bb_upper - bb_lower)   通道宽度为 0 时 NULL
--   amplitude     = (high - low) / 前收 * 100              -- 振幅 %，源不提供时用 OHLC 推导
--   pct_change    = (close - 前收) / 前收 * 100
--   price_change  = close - 前收
--
-- 为什么先取整再算派生列
--   Python 是 round(ma/bb, 3) 之后才用这两条轨算 dist_to_lower / band_pos，
--   若这里用未取整的中间值，会稳定产生 0.001 级别的偏差。
--
-- 一个重要限制
--   窗口只在「库里已有的行」上滑动。若某标的的入库起点就是区间起点，
--   最早 59 行的 ma60（以及最早 19 行的布林带）必然是 NULL。
--   需要区间头部也有指标时，把预热行一起入库：etf_daily.py --calc-db 已这样处理。
--
-- 幂等：只覆盖 8 个指标列，并按需补 NULL 的 amplitude / pct_change / price_change；
--       可重复执行，不会插入或删除任何行情行。
-- ============================================================================

CREATE PROCEDURE sp_etf_indicators(
    IN p_symbol   VARCHAR(16),
    IN p_adjust   VARCHAR(8),
    IN p_fill_pct TINYINT
)
MODIFIES SQL DATA
COMMENT '重算 ETF 均线/布林带/派生列（幂等，可重复执行）'
BEGIN
    DROP TEMPORARY TABLE IF EXISTS tmp_etf_ind;

    CREATE TEMPORARY TABLE tmp_etf_ind (
        symbol        VARCHAR(6)    NOT NULL,
        adjust        VARCHAR(4)    NOT NULL,
        trade_date    DATE          NOT NULL,
        ma5           DECIMAL(10,3) NULL,
        ma20          DECIMAL(10,3) NULL,
        ma60          DECIMAL(10,3) NULL,
        bb_upper      DECIMAL(10,3) NULL,
        bb_mid        DECIMAL(10,3) NULL,
        bb_lower      DECIMAL(10,3) NULL,
        dist_to_lower DECIMAL(10,3) NULL,
        band_pos      DECIMAL(8,4)  NULL,
        PRIMARY KEY (symbol, adjust, trade_date)
    ) ENGINE=InnoDB;

    INSERT INTO tmp_etf_ind
        (symbol, adjust, trade_date, ma5, ma20, ma60,
         bb_upper, bb_mid, bb_lower, dist_to_lower, band_pos)
    SELECT
        symbol, adjust, trade_date, ma5, ma20, ma60, bb_upper, bb_mid, bb_lower,
        ROUND(close - bb_lower, 3) AS dist_to_lower,
        CASE WHEN bb_upper > bb_lower
             THEN ROUND((close - bb_lower) / (bb_upper - bb_lower), 4)
        END AS band_pos
    FROM (
        SELECT
            symbol, adjust, trade_date, close,
            CASE WHEN n5  = 5  THEN ROUND(a5,  3) END AS ma5,
            CASE WHEN n20 = 20 THEN ROUND(a20, 3) END AS ma20,
            CASE WHEN n60 = 60 THEN ROUND(a60, 3) END AS ma60,
            CASE WHEN n20 = 20 THEN ROUND(a20 + 2 * s20, 3) END AS bb_upper,
            CASE WHEN n20 = 20 THEN ROUND(a20, 3)           END AS bb_mid,
            CASE WHEN n20 = 20 THEN ROUND(a20 - 2 * s20, 3) END AS bb_lower
        FROM (
            SELECT
                symbol, adjust, trade_date, close,
                COUNT(*)         OVER w5  AS n5,
                AVG(close)       OVER w5  AS a5,
                COUNT(*)         OVER w20 AS n20,
                AVG(close)       OVER w20 AS a20,
                STDDEV_POP(close) OVER w20 AS s20,
                COUNT(*)         OVER w60 AS n60,
                AVG(close)       OVER w60 AS a60
            FROM etf_daily
            WHERE (p_symbol IS NULL OR p_symbol = '' OR symbol = p_symbol)
              AND (p_adjust IS NULL OR p_adjust = '' OR adjust = p_adjust)
            WINDOW
                w5  AS (PARTITION BY symbol, adjust ORDER BY trade_date
                        ROWS BETWEEN 4  PRECEDING AND CURRENT ROW),
                w20 AS (PARTITION BY symbol, adjust ORDER BY trade_date
                        ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
                w60 AS (PARTITION BY symbol, adjust ORDER BY trade_date
                        ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
        ) base
    ) calc;

    -- 只覆盖指标列，行情原始列（open/high/low/close/volume/...）不动
    UPDATE etf_daily t
      JOIN tmp_etf_ind i
        ON i.symbol     = t.symbol
       AND i.adjust     = t.adjust
       AND i.trade_date = t.trade_date
       SET t.ma5          = i.ma5,
           t.ma20         = i.ma20,
           t.ma60         = i.ma60,
           t.bb_upper     = i.bb_upper,
           t.bb_mid       = i.bb_mid,
           t.bb_lower     = i.bb_lower,
           t.dist_to_lower = i.dist_to_lower,
           t.band_pos     = i.band_pos;

    -- 前收盘落临时表：涨跌幅与振幅都要用，且避免把窗口函数直接塞进 UPDATE 的子查询
    DROP TEMPORARY TABLE IF EXISTS tmp_etf_prev;

    CREATE TEMPORARY TABLE tmp_etf_prev (
        symbol     VARCHAR(6)    NOT NULL,
        adjust     VARCHAR(4)    NOT NULL,
        trade_date DATE          NOT NULL,
        prev_close DECIMAL(10,3) NULL,
        PRIMARY KEY (symbol, adjust, trade_date)
    ) ENGINE=InnoDB;

    INSERT INTO tmp_etf_prev (symbol, adjust, trade_date, prev_close)
    SELECT symbol, adjust, trade_date,
           LAG(close) OVER (PARTITION BY symbol, adjust ORDER BY trade_date)
    FROM etf_daily
    WHERE (p_symbol IS NULL OR p_symbol = '' OR symbol = p_symbol)
      AND (p_adjust IS NULL OR p_adjust = '' OR adjust = p_adjust);

    -- 涨跌额/涨跌幅：只补 NULL，不覆盖数据源给的原始值
    IF p_fill_pct = 1 THEN
        UPDATE etf_daily t
          JOIN tmp_etf_prev p
            ON p.symbol     = t.symbol
           AND p.adjust     = t.adjust
           AND p.trade_date = t.trade_date
           SET t.price_change = ROUND(t.close - p.prev_close, 3),
               t.pct_change   = ROUND((t.close - p.prev_close) / p.prev_close * 100, 3)
         WHERE p.prev_close IS NOT NULL
           AND p.prev_close <> 0
           AND (t.pct_change IS NULL OR t.price_change IS NULL);
    END IF;

    -- 振幅 %：新浪/腾讯源都不返回该字段，东财才有，所以只用 OHLC 推导补 NULL，
    -- 不覆盖数据源原始值。序列首个交易日没有前收，保持 NULL（无法推导）。
    UPDATE etf_daily t
      JOIN tmp_etf_prev p
        ON p.symbol     = t.symbol
       AND p.adjust     = t.adjust
       AND p.trade_date = t.trade_date
       SET t.amplitude = ROUND((t.high - t.low) / p.prev_close * 100, 3)
     WHERE p.prev_close IS NOT NULL
       AND p.prev_close <> 0
       AND t.high IS NOT NULL
       AND t.low  IS NOT NULL
       AND t.amplitude IS NULL;

    DROP TEMPORARY TABLE IF EXISTS tmp_etf_prev;

    -- 回执：Python 侧 fetchall() 读取，顺便确认影响范围
    SELECT p_symbol AS symbol, p_adjust AS adjust,
           COUNT(*) AS rows_total,
           SUM(ma60 IS NOT NULL) AS rows_ma60,
           MIN(trade_date) AS first_date,
           MAX(trade_date) AS last_date
    FROM tmp_etf_ind;

    DROP TEMPORARY TABLE IF EXISTS tmp_etf_ind;
END
