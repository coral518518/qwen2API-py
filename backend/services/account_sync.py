import httpx
import logging
import asyncio
from backend.core.account_pool import AccountPool, Account

log = logging.getLogger("qwen2api.account_sync")


async def do_sync_accounts(pool: AccountPool):
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://d1.coral001.de5.net/common_data/kvselect/qwen", timeout=15.0
            )
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data, list) and len(data) > 0:
            async with pool._lock:
                existing_map = {acc.email: acc for acc in pool.accounts}
                added_count = 0
                updated_count = 0

                for d in data:
                    email = d.get("email")
                    if not email:
                        continue

                    if email in existing_map:
                        acc = existing_map[email]
                        if "password" in d:
                            acc.password = d["password"]
                        if "token" in d:
                            acc.token = d["token"]
                        if "cookies" in d:
                            acc.cookies = d["cookies"]
                        if "username" in d:
                            acc.username = d["username"]
                        if "status_code" in d:
                            acc.status_code = d["status_code"]
                        if "activation_pending" in d:
                            acc.activation_pending = d["activation_pending"]
                            if d["activation_pending"]:
                                acc.valid = False
                        updated_count += 1
                    else:
                        pool.accounts.append(Account(**d))
                        added_count += 1

            await pool.save()
            log.info(
                f"[Account Sync] 同步成功，新增 {added_count} 个，更新 {updated_count} 个账号"
            )
            return True, f"同步成功，新增 {added_count} 个，更新 {updated_count} 个账号"

        log.warning("[Account Sync] 接口未返回有效数据或数组为空")
        return False, "接口未返回有效数据或数组为空"

    except httpx.RequestError as e:
        log.error(f"[Account Sync] 请求接口失败: {str(e)}")
        return False, f"请求接口失败: {str(e)}"
    except Exception as e:
        log.error(f"[Account Sync] 同步异常: {str(e)}")
        return False, f"同步异常: {str(e)}"


async def auto_sync_loop(pool: AccountPool):
    # 启动等待一段时间确保所有服务都已就绪
    await asyncio.sleep(30)
    # while True:
    log.info("[Account Sync] 开始执行自动同步账号...")
    await do_sync_accounts(pool)
    # 每 24 小时执行一次同步
    # await asyncio.sleep(86400)
