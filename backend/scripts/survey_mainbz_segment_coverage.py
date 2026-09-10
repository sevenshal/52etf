#!/usr/bin/env python3
"""普查 tushare 主营业务构成(fina_mainbz)的披露颗粒度，决定分部估值(SOTP)值不值得做。

**为什么先普查再动手**：分部估值需要按业务线拆开的收入和利润，而 tushare 唯一的
分部数据接口就是 `fina_mainbz`(2000积分，全市场版 fina_mainbz_vip 需 5000积分)，
字段只有 `bz_item`(分部名) / `bz_sales`(收入) / `bz_cost`(成本) / `bz_profit`(利润，
通常是收入−成本的毛利口径)。它**没有**分部净利、分部资产、分部资本开支，所以能做的
只是相对估值型 SOTP(EV/Sales、EV/毛利)，做不了分部 DCF / 分部 PE / 分部 ROIC。

更要命的是披露颗粒度：A股不少制造业公司的"分产品"表只有一行。已知的实例是长电科技
(600584.SH)——2025年报「主营业务分产品」就一行"芯片封测 388.71亿"，对它跑 SOTP
无从谈起(那份分部估值只能从年报的「主要控股参股公司分析」子公司表里手工拆，
而 tushare 没有任何接口提供那张表)。

所以先量：全市场有多少公司真的拆得开？判据是
  - 分部条目数 >= --min-items(默认3)
  - 最大单项收入占比 < --max-share(默认0.7)
两条都满足才算"可做 SOTP"。跑完再决定要不要建同步管道。

**只读**，不写任何数据库。需要在有 tushare token 的环境里跑(即生产环境本身)：

    cd backend && ../.venv/bin/python scripts/survey_mainbz_segment_coverage.py
    ../.venv/bin/python scripts/survey_mainbz_segment_coverage.py --limit 300 --type P
    ../.venv/bin/python scripts/survey_mainbz_segment_coverage.py --out /tmp/mainbz.csv
"""
import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.tushare import TushareService  # noqa: E402


def _latest_period_rows(frame):
    """一只股票可能返回多期，只看最近一期报告期。"""
    if frame is None or frame.empty or "end_date" not in frame.columns:
        return frame
    latest = frame["end_date"].max()
    return frame[frame["end_date"] == latest]


def _summarise(ts_code, name, frame):
    rows = _latest_period_rows(frame)
    if rows is None or rows.empty:
        return {"ts_code": ts_code, "name": name, "period": None, "items": 0,
                "top_share": None, "total_sales": None, "sotp_ready": False}
    sales = [float(value) for value in rows.get("bz_sales", []) if value is not None and float(value) > 0]
    total = sum(sales)
    return {
        "ts_code": ts_code,
        "name": name,
        "period": str(rows["end_date"].iloc[0]),
        "items": int(len(rows)),
        "top_share": round(max(sales) / total, 4) if sales and total > 0 else None,
        "total_sales": total or None,
        "sotp_ready": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200, help="抽样股票数量(默认200，0=全市场)")
    parser.add_argument("--type", default="P", choices=["P", "D", "I"], help="P产品/D地区/I行业")
    parser.add_argument("--min-items", type=int, default=3, help="算作可做SOTP的最少分部条目数")
    parser.add_argument("--max-share", type=float, default=0.7, help="最大单项收入占比上限")
    parser.add_argument("--out", help="把逐股明细写成 CSV")
    args = parser.parse_args()

    try:
        service = TushareService()
    except ValueError as exc:
        print(f"没有可用的 tushare token：{exc}", file=sys.stderr)
        print("这个脚本要在配置了 token 的环境里跑（生产环境，或设置 TUSHARE_TOKEN 环境变量）。",
              file=sys.stderr)
        return 1
    basic = service.get_a_stock_basic_frame(list_statuses=["L"])  # 只看在市股票
    if basic is None or basic.empty:
        print("拿不到股票列表，先确认 tushare token 配置", file=sys.stderr)
        return 1
    universe = basic[["ts_code", "name"]].to_dict("records")
    if args.limit:
        universe = universe[: args.limit]

    results, failures = [], Counter()
    for index, row in enumerate(universe, start=1):
        ts_code = row["ts_code"]
        try:
            frame = service.pro.fina_mainbz(ts_code=ts_code, type=args.type)
        except Exception as exc:  # 权限/积分/限流都在这里暴露
            failures[str(exc)[:120]] += 1
            continue
        summary = _summarise(ts_code, row.get("name"), frame)
        summary["sotp_ready"] = bool(
            summary["items"] >= args.min_items
            and summary["top_share"] is not None
            and summary["top_share"] < args.max_share
        )
        results.append(summary)
        if index % 50 == 0:
            print(f"  ...已处理 {index}/{len(universe)}", file=sys.stderr)

    if not results:
        print("一条都没取到。常见原因：积分不足(fina_mainbz 需 2000 分)、或接口被限流。", file=sys.stderr)
        for message, count in failures.most_common(5):
            print(f"  {count}x {message}", file=sys.stderr)
        return 1

    covered = [item for item in results if item["items"] > 0]
    ready = [item for item in covered if item["sotp_ready"]]
    single = [item for item in covered if item["items"] == 1]
    item_counts = Counter(item["items"] for item in covered)

    print(f"\n=== fina_mainbz 覆盖度普查（type={args.type}，样本 {len(results)} 只）===")
    print(f"有分部披露            : {len(covered)} ({len(covered)/len(results):.1%})")
    print(f"只有 1 个分部(拆不开) : {len(single)} ({len(single)/max(1,len(covered)):.1%} of 有披露)")
    print(f"可做SOTP(条目>={args.min_items} 且最大项<{args.max_share:.0%}) : "
          f"{len(ready)} ({len(ready)/max(1,len(covered)):.1%} of 有披露)")
    print("\n分部条目数分布：")
    for items, count in sorted(item_counts.items()):
        print(f"  {items:>2} 个分部: {count:>4} 只")
    if failures:
        print("\n失败样本(前5类)：")
        for message, count in failures.most_common(5):
            print(f"  {count}x {message}")

    print("\n可做SOTP的样本(前15只)：")
    for item in sorted(ready, key=lambda x: -(x["total_sales"] or 0))[:15]:
        print(f"  {item['ts_code']} {item['name']:<8} {item['items']} 个分部，"
              f"最大项占比 {item['top_share']:.1%}")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()))
            writer.writeheader()
            writer.writerows(results)
        print(f"\n明细已写入 {args.out}")

    print("\n判读：可做SOTP的比例低于两三成的话，建同步管道不划算——"
          "更值得做的是对少数控股型/多元化公司单独处理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
