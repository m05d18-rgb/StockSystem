"""一次性遷移:把早期 legacy 20 日展望的「未結算」預測改成現行 10 日尺度。

背景:現行 _save_prediction_row 一律寫 MONSTER_TARGET_HORIZON_DAYS(=10),不再產生 20 日;
但 DB 殘留一批 2026-06-23~24 的 target_horizon=20、全 hit IS NULL 的 legacy 列。20 日展望要
湊滿 20 根未來K才成熟,比 10 日多等約 10 個交易日、更易變殭屍;且未來數週會陸續滑過 25 天
cutoff、又因該股仍在更新→被算成 real_overdue,假性墊高 overdue、誤觸「結算管線故障」警示。
改成 10 日對齊目標尺度、更快成熟。

安全:硬帶 `AND hit IS NULL`——只碰從未結算的列,不重算任何已結算 hit(零校準破壞)。
idempotent:再跑一次沒有符合列就 0 更新。走 backend.connect() 短交易。

執行:python tools/migrate_prediction_horizon.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import backend

try:
    from ml_backend import MONSTER_TARGET_HORIZON_DAYS as TARGET
except Exception:
    TARGET = 10


def migrate():
    with backend.connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE target_horizon = 20 AND hit IS NULL"
        ).fetchone()[0]
        conn.execute(
            # AND hit IS NULL 是硬性守衛:絕不動任何已結算列,避免重算 hit 破壞 12.6% 校準。
            "UPDATE predictions SET target_horizon = ? WHERE target_horizon = 20 AND hit IS NULL",
            (TARGET,),
        )
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE target_horizon = 20 AND hit IS NULL"
        ).fetchone()[0]
    return before, remaining


if __name__ == "__main__":
    changed, remaining = migrate()
    print(f"已把 {changed} 筆 legacy 20日未結算預測改成 {TARGET} 日尺度(AND hit IS NULL 守衛)")
    print(f"剩餘 target_horizon=20 且未結算: {remaining}(應為 0)")
