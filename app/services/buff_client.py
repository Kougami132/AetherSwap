import hashlib
import threading
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app.services.buff_auth import get_buff_auth_lock
from buff import (
    BuffBuyer,
    BuffWriteResultUnknown,
    PAY_METHOD_ALIPAY,
    PAY_METHOD_BALANCE,
    PAY_METHOD_NAMES,
    PAY_METHOD_WECHAT,
)


logger = logging.getLogger(__name__)
buff_timeout = 15
BUFF_ACCOUNT_ID = "default"


def count_lowest_price_orders(orders: List[dict]) -> Tuple[float, int]:
    if not orders:
        return 0.0, 0
    lowest = float(orders[0].get("price", 0))
    if lowest <= 0:
        return 0.0, 0
    count = 0
    for order in orders:
        try:
            price = float(order.get("price", 0))
        except (ValueError, TypeError):
            continue
        if abs(price - lowest) < 1e-6:
            count += 1
        elif price < lowest:
            lowest = price
            count = 1
    return lowest, count


def first_order_at_price(orders: List[dict], price: float) -> Optional[dict]:
    for order in orders:
        try:
            order_price = float(order.get("price", 0))
        except (ValueError, TypeError):
            continue
        if abs(order_price - price) < 1e-6:
            return order
    return None


class BuffClient:
    """Generation-aware BUFF facade.

    One authentication lock covers each logical operation so browser login,
    keepalive and checkout cannot mutate the same account session concurrently.
    Non-idempotent writes intentionally have no generic retry decorator.
    """

    # BUFF's current batch checkout requires preview state, a server-issued
    # batch_id/trace and conditional password_token.  The former implementation
    # did not implement that contract, so production must safely use single
    # checkout until the full flow can be verified end to end.
    supports_batch_buy = False

    def __init__(
        self,
        cookies: str,
        pay_method: str = "alipay",
        timeout_sec: int = buff_timeout,
        *,
        user_agent: Optional[str] = None,
        credential_generation: int = 0,
        credentials_provider: Optional[Callable[[], dict]] = None,
        credentials_update_callback: Optional[Callable[[str, str], None]] = None,
        balance_fallback_pay_method: str = "wechat",
    ) -> None:
        self._pay_method = self._normalize_pay_method(pay_method)
        self._pay_method_id = PAY_METHOD_NAMES[self._pay_method]
        self._balance_fallback_pay_method = self._normalize_balance_fallback(
            balance_fallback_pay_method
        )
        self._balance_fallback_pay_method_id = PAY_METHOD_NAMES[
            self._balance_fallback_pay_method
        ]
        self._timeout = timeout_sec
        self._credentials_provider = credentials_provider
        self._credentials_update_callback = credentials_update_callback
        self._credential_generation = self._as_generation(credential_generation)
        self._cookies = cookies or ""
        self._user_agent = (user_agent or "").strip() or None
        self._steam_id = ""
        self._client_lock = threading.RLock()
        self._auth_lock = get_buff_auth_lock()
        self._buyer = self._new_buyer(self._cookies, self._user_agent)

    @staticmethod
    def _normalize_pay_method(value: Any) -> str:
        method = str(value or "alipay").strip().lower()
        if method not in PAY_METHOD_NAMES:
            logger.warning(
                "未知 BUFF 支付方式 %r，已回退为 alipay",
                value,
            )
            return "alipay"
        return method

    @staticmethod
    def _normalize_balance_fallback(value: Any) -> str:
        method = str(value or "wechat").strip().lower()
        if method not in {"wechat", "alipay"}:
            logger.warning(
                "未知 BUFF 余额不足备用支付方式 %r，已回退为 wechat",
                value,
            )
            return "wechat"
        return method

    @staticmethod
    def _as_generation(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _new_buyer(self, cookies: str, user_agent: Optional[str]) -> BuffBuyer:
        # 优先使用本地"当前 Steam 账号"里配置的 steam_id 作为收货账号，
        # 这样多绑 Steam 时 BUFF 下单请求的 steamid 字段能跟随用户在 UI
        # 中的选择，而不是被 BUFF 服务端默认的第一个账号覆盖。
        from app.accounts import get_current_account

        override_sid = ""
        try:
            acc = get_current_account() or {}
            override_sid = str(acc.get("steam_id") or "").strip()
        except Exception:
            override_sid = ""
        effective_sid = override_sid or self._steam_id
        return BuffBuyer(
            cookies,
            pay_method=self._pay_method_id,
            user_agent=user_agent,
            account_id=BUFF_ACCOUNT_ID,
            request_timeout=self._timeout,
            steam_id=effective_sid,
        )

    def _ensure_current_buyer(self) -> BuffBuyer:
        # 除了 BUFF 凭据本身，还要跟踪"本地当前 Steam 账号 steam_id"的变化。
        # 用户在 UI 切换 Steam 账号后，即使 BUFF cookie/generation 没变，
        # 也需要重建 buyer 以让新的收货 steam_id 立即生效。
        from app.accounts import get_current_account

        current_sid = ""
        try:
            acc = get_current_account() or {}
            current_sid = str(acc.get("steam_id") or "").strip()
        except Exception:
            current_sid = ""

        if self._credentials_provider is None:
            buyer_sid = getattr(self._buyer, "steam_id", None)
            if current_sid and buyer_sid is not None and current_sid != (buyer_sid or ""):
                old_buyer = self._buyer
                self._buyer = self._new_buyer(self._cookies, self._user_agent)
                close = getattr(old_buyer, "close", None)
                if callable(close):
                    close()
            return self._buyer

        credentials = self._credentials_provider() or {}
        generation = self._as_generation(credentials.get("generation"))
        cookies = str(credentials.get("cookies") or "")
        user_agent = str(credentials.get("user_agent") or "").strip() or None
        buyer_sid = (getattr(self._buyer, "steam_id", "") or "") if self._buyer is not None else ""
        if (
            generation == self._credential_generation
            and cookies == self._cookies
            and user_agent == self._user_agent
            and (not current_sid or current_sid == buyer_sid)
        ):
            return self._buyer

        old_buyer = self._buyer
        self._buyer = self._new_buyer(cookies, user_agent)
        self._cookies = cookies
        self._user_agent = user_agent
        self._credential_generation = generation
        close = getattr(old_buyer, "close", None)
        if callable(close):
            close()
        return self._buyer

    def _persist_rotated_cookies(self, buyer: BuffBuyer) -> None:
        if self._credentials_update_callback is None:
            return
        latest = buyer.export_cookie_string()
        if not latest or latest == self._cookies:
            return
        self._credentials_update_callback(latest, buyer.user_agent)
        self._cookies = latest
        if self._credentials_provider is not None:
            current = self._credentials_provider() or {}
            self._credential_generation = self._as_generation(
                current.get("generation")
            )

    def get_credential_identity(self) -> Dict[str, Any]:
        """Return a non-secret identity for checkout continuity checks."""

        with self._auth_lock:
            with self._client_lock:
                if self._credentials_provider is not None:
                    credentials = self._credentials_provider() or {}
                    generation = self._as_generation(credentials.get("generation"))
                    cookies = str(credentials.get("cookies") or "")
                    user_agent = (
                        str(credentials.get("user_agent") or "").strip() or None
                    )
                else:
                    generation = self._credential_generation
                    cookies = self._cookies
                    user_agent = self._user_agent
                digest = hashlib.sha256(
                    f"{cookies}\0{user_agent or ''}".encode(
                        "utf-8", errors="replace"
                    )
                ).hexdigest()
                return {
                    "credential_generation": generation,
                    "credential_fingerprint": digest,
                }

    def _run(self, operation: Callable[[BuffBuyer], Any]) -> Any:
        with self._auth_lock:
            with self._client_lock:
                buyer = self._ensure_current_buyer()
                try:
                    return operation(buyer)
                finally:
                    try:
                        self._persist_rotated_cookies(buyer)
                    except Exception as exc:
                        # Cookie persistence is important but must never replace
                        # a rate-limit/risk/write-unknown exception from BUFF.
                        logger.exception("持久化 BUFF 轮换 Cookie 失败: %s", exc)

    def close(self) -> None:
        with self._client_lock:
            close = getattr(self._buyer, "close", None)
            if callable(close):
                close()

    def get_sell_orders(self, goods_id: int, game: str = "csgo") -> Optional[list]:
        return self._run(lambda buyer: buyer.get_sell_orders(goods_id, game))

    def verify_session(self, game: str = "csgo") -> bool:
        def operation(buyer: BuffBuyer) -> bool:
            verified = bool(buyer.verify_session(game))
            if verified and buyer.steam_id:
                self._steam_id = buyer.steam_id
            return verified

        return bool(self._run(operation))

    def get_steam_trades(self) -> Optional[list]:
        return self._run(lambda buyer: buyer.get_steam_trades())

    def get_goods_steam_price_cny(
        self, search_name: str, game: str = "csgo"
    ) -> Optional[float]:
        return self._run(
            lambda buyer: buyer.get_goods_steam_price_cny(search_name, game)
        )

    def ask_seller_to_send(
        self, bill_order_id_or_ids: Union[str, List[str]], game: str = "csgo"
    ) -> bool:
        return self._run(
            lambda buyer: buyer.ask_seller_to_send(bill_order_id_or_ids, game)
        )

    def lock_and_get_pay_url(
        self,
        game: str,
        goods_id: int,
        sell_order_id: str,
        price: str,
        *,
        on_created: Optional[Callable[[str], None]] = None,
        preview: Optional[dict] = None,
        pay_method: Optional[str] = None,
    ) -> Dict[str, Any]:
        selected_method = self._normalize_pay_method(pay_method or self._pay_method)
        selected_method_id = PAY_METHOD_NAMES[selected_method]
        result = self._run(
            lambda buyer: buyer.lock_and_get_pay_url(
                game,
                goods_id,
                sell_order_id,
                price,
                on_created=on_created,
                preview=preview,
                pay_method=selected_method_id,
            )
        )

        # Keep the fallback safe even for callers that do not run the separate
        # prepare_single_buy step (for example legacy/compatibility callers),
        # and for a balance preview that becomes insufficient just before the
        # lock request. No order has been created in this branch, so retrying
        # with the configured external method cannot duplicate a checkout.
        if (
            self._pay_method == "balance"
            and selected_method == "balance"
            and isinstance(result, dict)
            and result.get("success") is not True
            and result.get("created") is False
            and (
                result.get("code") == "BALANCE_INSUFFICIENT"
                or result.get("safe_to_fallback") is True
            )
        ):
            fallback = self.balance_fallback_preview(
                result,
                game,
                goods_id,
                sell_order_id,
                price,
            )
            if isinstance(fallback, dict) and fallback.get("success"):
                logger.info(
                    "BUFF 余额支付不可用，切换到 %s 支付后重试锁单",
                    fallback.get("pay_method") or self._balance_fallback_pay_method,
                )
                return self.lock_and_get_pay_url(
                    game,
                    goods_id,
                    sell_order_id,
                    price,
                    on_created=on_created,
                    preview=fallback.get("preview"),
                    pay_method=fallback.get("pay_method"),
                )
            if isinstance(fallback, dict):
                logger.warning(
                    "BUFF 余额支付不可用，备用支付预检仍未通过: %s",
                    fallback.get("msg") or fallback.get("code") or "未知原因",
                )
                result = dict(result)
                result["fallback_preview"] = fallback
        return result

    def balance_fallback_pay_method(self) -> str:
        return self._balance_fallback_pay_method

    def balance_fallback_preview(
        self,
        preview_result: Dict[str, Any],
        game: str,
        goods_id: int,
        sell_order_id: str,
        price: str,
    ) -> Optional[Dict[str, Any]]:
        if self._pay_method != "balance":
            return None
        if not isinstance(preview_result, dict):
            return None
        if preview_result.get("success"):
            return None
        if preview_result.get("created") is not False:
            return None
        if (
            preview_result.get("code") != "BALANCE_INSUFFICIENT"
            and preview_result.get("safe_to_fallback") is not True
        ):
            return None
        fallback_method = self._balance_fallback_pay_method
        fallback_id = PAY_METHOD_NAMES[fallback_method]

        def operation(buyer: BuffBuyer) -> Dict[str, Any]:
            preview = buyer.preview_buy(game, goods_id, sell_order_id, price)
            if preview.get("code") != "OK":
                return {
                    "success": False,
                    "created": False,
                    "code": str(preview.get("code") or "PREVIEW_REJECTED"),
                    "msg": preview.get("error")
                    or preview.get("msg")
                    or "BUFF 备用支付预检未通过",
                }
            data = preview.get("data")
            if not isinstance(data, dict):
                return {
                    "success": False,
                    "created": False,
                    "code": "PREVIEW_INVALID",
                    "msg": "BUFF 备用支付预检返回格式异常",
                }
            error = buyer._preview_payment_error(data, fallback_id)
            if error:
                return {
                    "success": False,
                    "created": False,
                    "code": "PAY_METHOD_UNAVAILABLE",
                    "msg": error,
                }
            return {
                "success": True,
                "created": False,
                "preview": preview,
                "pay_method": fallback_method,
            }

        return self._run(operation)

    def prepare_single_buy(
        self,
        game: str,
        goods_id: int,
        sell_order_id: str,
        price: str,
    ) -> Dict[str, Any]:
        """Complete BUFF's read-only preview before a checkout intent is written."""

        def operation(buyer: BuffBuyer) -> Dict[str, Any]:
            preview = buyer.preview_buy(game, goods_id, sell_order_id, price)
            if preview.get("code") != "OK":
                return {
                    "success": False,
                    "created": False,
                    "code": str(preview.get("code") or "PREVIEW_REJECTED"),
                    "msg": preview.get("error")
                    or preview.get("msg")
                    or "BUFF 购买预检未通过",
                }
            data = preview.get("data")
            if not isinstance(data, dict):
                return {
                    "success": False,
                    "created": False,
                    "code": "PREVIEW_INVALID",
                    "msg": "BUFF 购买预检返回格式异常",
                }
            if self._pay_method == "balance":
                error = buyer._preview_balance_error(data)
            else:
                error = buyer._preview_payment_error(data)
            if error:
                code = "PAY_METHOD_UNAVAILABLE"
                safe_to_fallback = False
                if self._pay_method == "balance":
                    safe_to_fallback = True
                    if buyer.is_balance_insufficient_text(error):
                        code = "BALANCE_INSUFFICIENT"
                    else:
                        code = "BALANCE_UNAVAILABLE"
                return {
                    "success": False,
                    "created": False,
                    "code": code,
                    "msg": error,
                    "safe_to_fallback": safe_to_fallback,
                }
            return {"success": True, "created": False, "preview": preview}

        return self._run(operation)

    def try_batch_buy(
        self,
        goods_id: int,
        game: str,
        orders: List[dict],
        unit_price: float,
        num: int,
        *,
        on_created: Optional[Callable[[str], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        del goods_id, game, orders, unit_price, num, on_created
        return {
            "success": False,
            "code": "NOT_SUPPORTED",
            "created": False,
            "safe_to_fallback": True,
            "msg": "BUFF 批量购买协议已变化，已安全降级为单件购买",
        }

    def batch_buy_find_and_finalize(
        self,
        goods_id: int,
        game: str,
        max_price: float,
        num: int,
        batch_id: str,
        *,
        on_match: Optional[
            Callable[[Dict[str, Any], List[Dict[str, Any]]], None]
        ] = None,
    ) -> List[Dict[str, Any]]:
        def operation(buyer: BuffBuyer) -> List[Dict[str, Any]]:
            matched: List[Dict[str, Any]] = []
            seen_sell_order_ids = set()
            seen_bill_order_ids = set()
            try:
                orders = buyer.get_sell_orders(goods_id, game)
            except Exception as exc:
                setattr(exc, "partial_results", list(matched))
                setattr(exc, "batch_id", str(batch_id))
                raise
            if not orders:
                return []
            for order in orders:
                if len(matched) >= num:
                    break
                sell_order_id = str(order.get("id") or "").strip()
                if (
                    not sell_order_id
                    or sell_order_id == "0"
                    or sell_order_id in seen_sell_order_ids
                ):
                    continue
                try:
                    price = float(order.get("price", 0))
                except (ValueError, TypeError):
                    continue
                if price <= max_price:
                    seen_sell_order_ids.add(sell_order_id)
                    try:
                        bill_order_id = buyer.batch_buy_finalize(
                            game,
                            goods_id,
                            sell_order_id,
                            str(order.get("price", "")),
                            batch_id,
                        )
                    except Exception as exc:
                        # Preserve every already-created bill for immediate
                        # persistence by the pipeline before it halts.
                        setattr(exc, "partial_results", list(matched))
                        setattr(exc, "batch_id", str(batch_id))
                        raise
                    if bill_order_id:
                        normalized_bill_id = str(bill_order_id).strip()
                        if (
                            not normalized_bill_id
                            or normalized_bill_id == "0"
                            or normalized_bill_id in seen_bill_order_ids
                        ):
                            error = BuffWriteResultUnknown(
                                "BUFF 批量核销返回了空值或重复订单号，无法确认完整件数",
                                method="POST",
                            )
                            error.partial_results = list(matched)
                            error.batch_id = str(batch_id)
                            raise error
                        seen_bill_order_ids.add(normalized_bill_id)
                        match = {
                            "id": sell_order_id,
                            "price": price,
                            "bill_order_id": normalized_bill_id,
                        }
                        matched.append(match)
                        if on_match is not None:
                            try:
                                # Persist the newly-created external id before
                                # attempting another non-idempotent finalize.
                                on_match(dict(match), list(matched))
                            except Exception as exc:
                                setattr(exc, "partial_results", list(matched))
                                setattr(exc, "batch_id", str(batch_id))
                                raise
                    else:
                        # A definitive non-OK response may be safe for this
                        # sell order, but continuing through more POSTs in the
                        # same paid batch is not.  Return the known partial set
                        # and require reconciliation.
                        break
            return matched

        return self._run(operation)


def create_buff_client_from_config(credentials: dict, config: dict) -> BuffClient:
    from app.config_loader import get_buff_credentials, update_buff_creds

    credentials = credentials or {}
    buff_cfg = config.get("buff", {})

    def persist_rotated_cookies(cookies: str, user_agent: str) -> None:
        # Preserve the authentication source; only the server-issued CookieJar
        # and the UA bound to it are refreshed here.
        update_buff_creds(cookies, user_agent=user_agent)

    return BuffClient(
        str(credentials.get("cookies") or ""),
        pay_method=buff_cfg.get("pay_method", "alipay"),
        timeout_sec=buff_timeout,
        user_agent=str(credentials.get("user_agent") or "").strip() or None,
        credential_generation=credentials.get("generation", 0),
        credentials_provider=get_buff_credentials,
        credentials_update_callback=persist_rotated_cookies,
        balance_fallback_pay_method=buff_cfg.get(
            "balance_fallback_pay_method", "wechat"
        ),
    )
