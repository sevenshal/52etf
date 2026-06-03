from datetime import datetime, date
from ..core.database import (
    Session,
    StockEVC,
    StockTag,
    stock_tags,
    FetchLog,
)
import logging
from sqlalchemy import and_
from ..core.services.evc import EVCService


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
                    raise RuntimeError(f"EVC stock fetch aborted on page {page}: {e}") from e

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
            raise
        finally:
            self.db_session.close() 
