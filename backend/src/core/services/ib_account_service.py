import logging
import asyncio
import subprocess
import os
from ib_insync import IB
from typing import Dict, Optional

logger = logging.getLogger(__name__)

class IBAccountService:
    @staticmethod
    async def get_account_status(host: str, port: int, client_id: int) -> Dict:
        """获取 IB 账户实时状态 (线程隔离版，解决 Loop 冲突)"""
        # 使用 asyncio.to_thread 将同步的 IB 连接操作放到独立线程执行
        # 这确保了 ib_insync 在该线程内创建私有的 Loop，不会影响 FastAPI 主进程的 Loop
        return await asyncio.to_thread(IBAccountService._get_account_status_sync, host, port, client_id)

    @staticmethod
    def _get_account_status_sync(host: str, port: int, client_id: int) -> Dict:
        """同步获取 IB 账户状态 (由 to_thread 调用，内部使用 asyncio.run 保证 Loop 隔离)"""
        async def _internal_check():
            ib = IB()
            try:
                # 在私有 Loop 中进行异步连接
                await ib.connectAsync(host, port, clientId=client_id, timeout=5)
                
                # 获取账户值
                account_values = {v.tag: v.value for v in ib.accountValues()}
                
                # 提取关键指标
                return {
                    "connected": True,
                    "net_liquidation": float(account_values.get('NetLiquidation', '0') or 0),
                    "available_funds": float(account_values.get('AvailableFunds', '0') or 0),
                    "gross_position_value": float(account_values.get('GrossPositionValue', '0') or 0),
                    "daily_pnl": float(account_values.get('DailyPnL', '0') or 0),
                    "currency": account_values.get('Currency', 'USD'),
                    "message": "Connected"
                }
            except Exception as e:
                logger.error(f"Internal IB check error for {host}:{port}: {e}")
                return {
                    "connected": False,
                    "net_liquidation": 0,
                    "available_funds": 0,
                    "gross_position_value": 0,
                    "daily_pnl": 0,
                    "message": str(e)
                }
            finally:
                if ib.isConnected():
                    ib.disconnect()

        # asyncio.run 会为当前线程创建一个全新的 Loop，执行完后自动关闭
        try:
            return asyncio.run(_internal_check())
        except Exception as e:
            logger.error(f"asyncio.run fallback error for {host}:{port}: {e}")
            return {
                "connected": False,
                "net_liquidation": 0,
                "available_funds": 0,
                "gross_position_value": 0,
                "daily_pnl": 0,
                "message": f"Loop error: {str(e)}"
            }

    @staticmethod
    def restart_gateway(container_name: str) -> Dict:
        """重启对应的 Docker 容器"""
        if not container_name:
            return {"success": False, "message": "Container name is empty"}
            
        try:
            # 获取 docker 二进制文件路径，如果没有在 PATH 中
            docker_bin = os.getenv('DOCKER_BINARY_PATH', 'docker')
            
            # 执行重启命令
            result = subprocess.run(
                [docker_bin, "restart", container_name],
                capture_output=True,
                text=True,
                check=True
            )
            
            logger.info(f"Docker restart success: {container_name}")
            return {"success": True, "message": f"Container {container_name} restarted"}
        except subprocess.CalledProcessError as e:
            logger.error(f"Docker restart failed: {e.stderr}")
            return {"success": False, "message": e.stderr or str(e)}
        except Exception as e:
            logger.error(f"Docker restart error: {e}")
            return {"success": False, "message": str(e)}

    @staticmethod
    def deploy_gateway(config) -> Dict:
        """根据配置部署/更新 Docker 容器"""
        if not config.container_name:
            return {"success": False, "message": "Container name is required for deployment"}
            
        try:
            docker_bin = os.getenv('DOCKER_BINARY_PATH', 'docker')
            
            # 1. 停止并删除旧容器 (如果存在)
            logger.info(f"Removing existing container: {config.container_name}")
            subprocess.run([docker_bin, "rm", "-f", config.container_name], capture_output=True)
            
            # 2. 构建 run 命令
            # 映射 API 端口：宿主机的 config.ib_port -> 容器内的 4003(live) / 4004(paper)
            internal_port = 4004 if config.trading_mode == 'paper' else 4003
            
            cmd = [
                docker_bin, "run", "-d",
                "--name", config.container_name,
                "--memory", "512m",
                "--memory-swap", "768m",
                "--restart", "always",
                "-e", "JAVA_HEAP_SIZE=384",
                "-e", f"TWS_USERID={config.tws_userid}",
                "-e", f"TWS_PASSWORD={config.tws_password}",
                "-e", f"TRADING_MODE={config.trading_mode}",
                "-e", f"TWS_ACCEPT_INCOMING=yes",
                "-e", "READ_ONLY_API=no",
                "-e", f"TWOFA_TIMEOUT_ACTION={config.twofa_timeout_action}",
                "-e", f"AUTO_RESTART_TIME={config.auto_restart_time}",
                "-e", f"RELOGIN_AFTER_TWOFA_TIMEOUT={config.relogin_after_twofa_timeout}",
            ]
            
            # 如果是 paper 模式，设置具体的 paper 环境变量
            if config.trading_mode == 'paper':
                cmd.extend([
                    "-e", f"TWS_USERID_PAPER={config.tws_userid}",
                    "-e", f"TWS_PASSWORD_PAPER={config.tws_password}"
                ])
                
            # 添加端口映射和镜像名 (镜像名必须放在最后，或者后面跟镜像的启动参数)
            cmd.extend([
                "-p", f"{config.ib_port}:{internal_port}",
                "ghcr.io/gnzsnz/ib-gateway:latest"
            ])

            # 3. 记录日志 (注意屏蔽密码)
            safe_cmd = [arg if "PASSWORD" not in arg else f"{arg.split('=')[0]}=******" for arg in cmd]
            logger.info(f"Executing: {' '.join(safe_cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            return {"success": True, "message": f"Container {config.container_name} deployed. ID: {result.stdout[:12]}"}
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Docker deploy failed: {e.stderr}")
            return {"success": False, "message": e.stderr or str(e)}
        except Exception as e:
            logger.error(f"Docker deploy error: {e}")
            return {"success": False, "message": str(e)}
