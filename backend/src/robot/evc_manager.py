from copy import deepcopy
from datetime import datetime, date
from typing import Dict, Optional
from ..core.database import (
    Session,
    StockEVC,
    StockTag,
    stock_tags,
    FetchLog,
    StockStaticInfoSnapshot,
    StockStaticInfoHistory,
)
import logging
from sqlalchemy import and_
from ..core.static_info import STATIC_INFO_FIELDS
from ..core.services.evc import EVCService
from ..core.services.longport import LongPortService

STATIC_INFO_SYNC_BATCH_SIZE = 500


def _normalize_static_info_payload(static_info: Dict) -> Dict:
    stock_derivatives = static_info.get("stock_derivatives") or []
    if not isinstance(stock_derivatives, (list, tuple, set)):
        stock_derivatives = [stock_derivatives] if stock_derivatives else []
    normalized_derivatives = sorted(
        dict.fromkeys(
            str(item)
            for item in stock_derivatives
            if item is not None and str(item)
        )
    )
    return {
        "symbol": static_info.get("symbol"),
        "name_cn": static_info.get("name_cn"),
        "name_en": static_info.get("name_en"),
        "name_hk": static_info.get("name_hk"),
        "exchange": static_info.get("exchange"),
        "currency": static_info.get("currency"),
        "lot_size": static_info.get("lot_size"),
        "total_shares": static_info.get("total_shares"),
        "circulating_shares": static_info.get("circulating_shares"),
        "hk_shares": static_info.get("hk_shares"),
        "eps": static_info.get("eps"),
        "eps_ttm": static_info.get("eps_ttm"),
        "bps": static_info.get("bps"),
        "dividend_yield": static_info.get("dividend_yield"),
        "stock_derivatives": normalized_derivatives,
        "board": static_info.get("board"),
    }


def _payload_from_record(record) -> Dict:
    return {field: deepcopy(getattr(record, field, None)) for field in STATIC_INFO_FIELDS}


def _create_static_info_model(model_cls, payload: Dict, record_date: date, now: datetime, created_at: Optional[datetime] = None):
    kwargs = {field: deepcopy(payload.get(field)) for field in STATIC_INFO_FIELDS}
    kwargs.update({
        "date": record_date,
        "raw_data": deepcopy(payload),
        "created_at": created_at or now,
        "updated_at": now,
    })
    return model_cls(**kwargs)

class EVCManager:
    """EVC数据管理器
    
    负责:
    1. 股票估值数据的获取和存储
    2. 标签数据的管理
    3. 数据分析和更新
    """
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.db_session = Session()
        self.evc_service = EVCService()

    def _iter_evc_symbols(self, batch_size: int = STATIC_INFO_SYNC_BATCH_SIZE):
        offset = 0
        while True:
            rows = (
                self.db_session.query(StockEVC.symbol)
                .distinct()
                .order_by(StockEVC.symbol)
                .offset(offset)
                .limit(batch_size)
                .all()
            )
            symbols = [row[0] for row in rows if row and row[0]]
            if not symbols:
                break
            yield symbols
            if len(symbols) < batch_size:
                break
            offset += batch_size

    def sync_static_info_snapshots(self, batch_size: int = STATIC_INFO_SYNC_BATCH_SIZE):
        """同步股票静态信息快照与历史记录。"""
        try:
            current_date = date.today()
            now = datetime.now()
            total_symbols = 0
            fetched_symbols = 0
            created_count = 0
            changed_count = 0
            refreshed_count = 0
            history_count = 0
            missing_count = 0
            quote_service = LongPortService.get_instance()

            for batch_index, symbols in enumerate(self._iter_evc_symbols(batch_size=batch_size), start=1):
                total_symbols += len(symbols)
                try:
                    static_infos = quote_service.get_static_info(symbols) if symbols else []
                except Exception as exc:
                    self.logger.error("Fetch static info batch %s failed: %s", batch_index, exc)
                    missing_count += len(symbols)
                    continue

                static_info_map = {}
                for info in static_infos or []:
                    symbol = info.get("symbol")
                    if not symbol:
                        continue
                    static_info_map[symbol] = _normalize_static_info_payload(info)

                snapshot_rows = (
                    self.db_session.query(StockStaticInfoSnapshot)
                    .filter(StockStaticInfoSnapshot.symbol.in_(symbols))
                    .all()
                )
                snapshot_map = {row.symbol: row for row in snapshot_rows}

                for symbol in symbols:
                    payload = static_info_map.get(symbol)
                    if not payload:
                        missing_count += 1
                        continue

                    fetched_symbols += 1
                    existing = snapshot_map.get(symbol)
                    if existing is None:
                        self.db_session.add(_create_static_info_model(
                            StockStaticInfoSnapshot,
                            payload,
                            current_date,
                            now,
                        ))
                        created_count += 1
                        continue

                    existing_payload = _payload_from_record(existing)
                    if existing_payload != payload:
                        self.db_session.merge(_create_static_info_model(
                            StockStaticInfoHistory,
                            existing_payload,
                            existing.date,
                            now,
                            created_at=existing.created_at or now,
                        ))
                        history_count += 1
                        changed_count += 1
                    else:
                        refreshed_count += 1

                    for field in STATIC_INFO_FIELDS:
                        setattr(existing, field, deepcopy(payload.get(field)))
                    existing.date = current_date
                    existing.raw_data = deepcopy(payload)
                    existing.updated_at = now

                self.db_session.commit()

            self.logger.info(
                "Static info snapshot sync completed: symbols=%s fetched=%s created=%s changed=%s refreshed=%s history=%s missing=%s",
                total_symbols,
                fetched_symbols,
                created_count,
                changed_count,
                refreshed_count,
                history_count,
                missing_count,
            )
            return {
                "symbols": total_symbols,
                "fetched": fetched_symbols,
                "created": created_count,
                "changed": changed_count,
                "refreshed": refreshed_count,
                "history": history_count,
                "missing": missing_count,
            }
        except Exception as e:
            self.db_session.rollback()
            self.logger.error(f"同步静态信息快照失败: {str(e)}")
            raise

    def fetch_and_stocks(self):
        """分页抓取所有股票数据并存储到数据库"""
        try:
            today = date.today()

            total_tags_fetched = 0
            total_stocks_fetched = 0

            # 首先获取并存储标签
            try:
                tags = self.evc_service.get_stock_tags()
                for tag_data in tags:
                    tag = StockTag(
                        id=tag_data.id,
                        created_at=tag_data.created_at,
                        name=tag_data.name,
                        built_in=tag_data.built_in,
                        official_only=tag_data.official_only,
                        includes_option_put_call=tag_data.includes_option_put_call,
                        option_put_call_fetch_tag_ordinal=tag_data.option_put_call_fetch_tag_ordinal,
                        sort_group=tag_data.sort_group,
                        updated_at=datetime.now()
                    )
                    self.db_session.merge(tag)
                self.db_session.commit()
                total_tags_fetched = len(tags)
                self.logger.info(f"Successfully stored/updated {total_tags_fetched} tags")
            except Exception as e:
                self.logger.error(f"Error fetching tags: {str(e)}")
                self.db_session.rollback()

            # 然后获取股票数据
            page = 1
            size = 60  # 每页数量
            total_processed = 0

            while True:
                try:
                    # 获取一页数据
                    stocks, ret_page, ret_total = self.evc_service.search_stock(
                        page=page, 
                        size=size, 
                        text="",
                        orderField="createdAt",
                        orderDirection="DESC"
                    )

                    if not stocks:
                        break

                    if page == 1 and ret_total > len(stocks) and len(stocks) < size:
                        self.logger.warning(
                            "EVC search returned only %s rows for page 1 while count=%s. "
                            "This usually means the cookie is missing/expired or the upstream API is preview-limiting the result.",
                            len(stocks),
                            ret_total,
                        )

                    for stock_data in stocks:
                        try:
                            # 使用merge直接更新或创建股票记录
                            stock = StockEVC(
                                symbol=stock_data.symbol,
                                date=today,
                                company=stock_data.company,
                                last_price=stock_data.last_price,
                                last_change=stock_data.last_change,
                                last_change_percent=stock_data.last_change_percent,
                                fair_value_lo=stock_data.fair_value_lo,
                                fair_value_hi=stock_data.fair_value_hi,
                                fair_value_date=stock_data.fair_value_date,
                                forward_next_fy_lo=stock_data.forward_next_fy_lo,
                                forward_next_fy_hi=stock_data.forward_next_fy_hi,
                                forward_next_fy_max_value_lo=stock_data.forward_next_fy_max_value_lo,
                                forward_next_fy_max_value_hi=stock_data.forward_next_fy_max_value_hi,
                                beta=stock_data.beta,
                                pe_ratio=stock_data.pe_ratio,
                                forward_pe_ratio=stock_data.forward_pe_ratio,
                                is_under=stock_data.is_under,
                                is_over=stock_data.is_over,
                                updated_at=datetime.now()
                            )
                            self.db_session.merge(stock)

                            # 清除并重新添加标签关联
                            self.db_session.execute(
                                stock_tags.delete().where(
                                    and_(
                                        stock_tags.c.stock_symbol == stock.symbol,
                                        stock_tags.c.date == today
                                    )
                                )
                            )

                            if stock_data.tags:
                                for tag_id in stock_data.tags:
                                    self.db_session.execute(
                                        stock_tags.insert().values(
                                            stock_symbol=stock.symbol,
                                            tag_id=tag_id,
                                            date=today
                                        )
                                    )

                            self.db_session.commit()
                            total_processed += 1
                            self.logger.info(f"Processed {stock_data.symbol} on {today}")

                        except Exception as e:
                            self.logger.error(f"Error processing stock {stock_data.symbol}: {str(e)}")
                            self.db_session.rollback()
                            continue

                    # 检查是否还有下一页
                    if len(stocks) < size or ret_page != page:
                        break

                    page += 1

                except Exception as e:
                    self.logger.error(f"Error fetching page {page}: {str(e)}")
                    break

            total_stocks_fetched = total_processed
            self.logger.info(f"Completed processing {total_stocks_fetched} stocks")

            # 记录拉取结果
            fetch_log = FetchLog(
                date=today,
                total_tags_fetched=total_tags_fetched,
                total_stocks_fetched=total_stocks_fetched
            )
            self.db_session.add(fetch_log)
            self.db_session.commit()

        except Exception as e:
            self.logger.error(f"Error in fetch_and_store_stocks: {str(e)}")
        finally:
            self.db_session.close() 
