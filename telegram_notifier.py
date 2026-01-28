"""
Telegram Notifier - Send notifications about trades and events.
Adapted from original strategy/telegram_notifier.py.
"""

import logging
from typing import Optional

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, LADDER_LEVELS, EXIT_PRICES, STOP_LOSS_PRICE
from models import EventContext, CycleResult, TrackedOrder, OrderType, MarketPhase

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Sends formatted Telegram notifications.
    KISS: Simple send_message + formatted helpers.
    """
    
    def __init__(self):
        self.enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
        self._session = requests.Session()
        
        if not self.enabled:
            logger.warning("⚠️ Telegram notifications disabled (missing credentials)")
    
    def send_message(self, message: str) -> bool:
        """Send a Telegram message."""
        if not self.enabled:
            return False
        
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            }
            
            response = self._session.post(url, json=payload, timeout=10)
            return response.status_code == 200
            
        except Exception as e:
            logger.error(f"❌ Telegram error: {e}")
            return False
    
    def send_startup(self, balance: float) -> bool:
        """Send bot startup notification."""
        message = (
            "🚀 *BOT PRODUCCIÓN INICIADO*\n\n"
            f"💰 Balance: ${balance:.2f}\n"
            f"📊 Niveles: {', '.join([str(int(l*100)) for l in LADDER_LEVELS])}¢\n"
            "🎯 Exits Dinámicos:\n"
            "  • 48→49¢ (+1¢)\n"
            "  • 46-47→48¢ (+1-2¢)\n"
            "  • 40-45→47¢ (+2-7¢)\n"
            f"🛡️ Stop-Loss: {int(STOP_LOSS_PRICE*100)}¢ (solo 48¢)\n"
            "⚡ Modo: REAL TRADING"
        )
        return self.send_message(message)
    
    def send_event_discovered(self, event: EventContext) -> bool:
        """Notify about new event discovery."""
        minutes = int(event.time_until_start() / 60)
        
        message = (
            f"🔍 *NUEVO EVENTO*\n\n"
            f"📅 `{event.slug}`\n"
            f"⏱️ LIVE en: {minutes} minutos"
        )
        return self.send_message(message)
    
    def send_ladder_placed(self, event_slug: str, order_count: int) -> bool:
        """Notify about ladder placement."""
        message = (
            f"🪜 *LADDER COLOCADA*\n\n"
            f"📅 `{event_slug}`\n"
            f"📊 Órdenes: {order_count}\n"
            f"💵 Niveles: {', '.join([str(int(l*100)) for l in LADDER_LEVELS])}¢\n"
            "🎯 Exits: 47-49¢ (dinámico)\n"
            f"🛡️ Stop: {int(STOP_LOSS_PRICE*100)}¢"
        )
        return self.send_message(message)
    
    def send_fill(self, order: TrackedOrder, pnl: Optional[float] = None) -> bool:
        """Notify about an order fill."""
        side_str = "COMPRA" if order.order_type == OrderType.BUY else "VENTA"
        
        lines = [
            "✅ *ORDEN EJECUTADA*",
            f"📅 `{order.event_slug}`",
            "",
            f"{order.side.display_name} | {side_str}",
            f"💵 Precio: {int(order.price*100)}¢",
            f"📦 Cantidad: {order.size} shares",
        ]
        
        if pnl is not None:
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            lines.append(f"💰 PnL: {pnl_str}")
        
        return self.send_message("\n".join(lines))
    
    def send_phase_transition(self, event: EventContext, cancelled_orders: int) -> bool:
        """Notify about event going LIVE."""
        message = (
            f"🔴 *EVENTO EN VIVO*\n\n"
            f"📅 `{event.slug}`\n"
            f"🛑 Compras canceladas: {cancelled_orders}\n"
            f"📤 Modo: Solo salidas"
        )
        return self.send_message(message)
    
    def send_cycle_report(self, result: CycleResult) -> bool:
        """Send cycle completion report."""
        lines = [
            "🎉 *CICLO COMPLETADO*",
            f"📅 `{result.event_slug}`",
            "",
            "*Fills Ejecutados:*"
        ]
        
        if result.fills_yes:
            yes_str = ', '.join([str(int(p*100)) for p in result.fills_yes])
            lines.append(f"🔼 YES: {yes_str}¢ ({len(result.fills_yes)} fills)")
        else:
            lines.append("🔼 YES: ---")
        
        if result.fills_no:
            no_str = ', '.join([str(int(p*100)) for p in result.fills_no])
            lines.append(f"🔽 NO: {no_str}¢ ({len(result.fills_no)} fills)")
        else:
            lines.append("🔽 NO: ---")
        
        lines.append("")
        lines.append("*💰 Resultado:*")
        
        pnl_str = f"+${result.total_pnl:.2f}" if result.total_pnl >= 0 else f"-${abs(result.total_pnl):.2f}"
        lines.append(f"PnL Realizado: {pnl_str}")
        
        if result.start_time and result.end_time:
            duration = int((result.end_time - result.start_time) / 60)
            lines.append(f"\n⏱️ Duración: {duration} minutos")
        
        return self.send_message("\n".join(lines))
    
    def send_error(self, error_msg: str) -> bool:
        """Send error notification."""
        message = f"❌ *ERROR*\n\n{error_msg}"
        return self.send_message(message)


# Singleton
_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """Get the singleton TelegramNotifier instance."""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
