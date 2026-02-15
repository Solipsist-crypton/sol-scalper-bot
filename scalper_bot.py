import telebot
from telebot import types
from kucoin.client import Market
import pandas as pd
import time
import threading
from datetime import datetime
import config

bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
client = Market()

class Position:
    def __init__(self, symbol, side, price, time):
        self.symbol = symbol
        self.side = side
        self.entry_price = price
        self.entry_time = time
        self.exit_price = None
        self.exit_time = None
        self.pnl_percent = None

class ScalperBot:
    def __init__(self):
        self.positions = {}          # {symbol: Position}
        self.last_state = {}          # {symbol: 'ABOVE'/'BELOW'}
        self.trades_history = []
        self.running = True
    
    def convert_symbol(self, symbol):
        return symbol.replace('USDT', '-USDT')
    
    def get_emas(self, symbol):
        try:
            kucoin_symbol = self.convert_symbol(symbol)
            klines = client.get_kline(
                symbol=kucoin_symbol,
                kline_type='1min',
                start_at=int(time.time()) - 150*60,
                end_at=int(time.time())
            )
            
            if not klines or len(klines) < 30:
                return None, None, None
            
            closes = [float(k[2]) for k in klines]
            df = pd.DataFrame(closes, columns=['close'])
            
            ema_fast = df['close'].ewm(span=12, adjust=False, min_periods=12).mean().iloc[-1]
            ema_slow = df['close'].ewm(span=26, adjust=False, min_periods=26).mean().iloc[-1]
            current_price = closes[-1]
            
            return ema_fast, ema_slow, current_price
        except Exception as e:
            print(f"Помилка для {symbol}: {e}")
            return None, None, None
    
    def check_crossover(self, symbol):
        """Перевіряє перетин EMA для пари"""
        ema_fast, ema_slow, price = self.get_emas(symbol)
        if not ema_fast:
            return None, None, None
        
        current_state = 'ABOVE' if ema_fast > ema_slow else 'BELOW'
        
        # Перший запуск - тільки запам'ятовуємо стан
        if symbol not in self.last_state:
            self.last_state[symbol] = current_state
            print(f"📊 {symbol}: початковий стан {current_state}")
            return None, None, price
        
        # ПЕРЕТИН! Стан змінився
        if current_state != self.last_state[symbol]:
            # Визначаємо сигнал
            if current_state == 'ABOVE':
                signal = 'LONG'
            else:
                signal = 'SHORT'
            
            # Запам'ятовуємо новий стан
            self.last_state[symbol] = current_state
            
            return signal, current_state, price
        
        return None, None, price
    
    def close_position(self, symbol, exit_price, exit_time):
        """Закриває позицію і рахує результат"""
        if symbol in self.positions:
            pos = self.positions[symbol]
            pos.exit_price = exit_price
            pos.exit_time = exit_time
            
            # Рахуємо PnL у відсотках
            if pos.side == 'LONG':
                pos.pnl_percent = ((exit_price - pos.entry_price) / pos.entry_price) * 100
            else:  # SHORT
                pos.pnl_percent = ((pos.entry_price - exit_price) / pos.entry_price) * 100
            
            # Рахуємо час утримання (в хвилинах)
            hold_minutes = (exit_time - pos.entry_time) / 60
            
            # Додаємо в історію
            trade_info = {
                'symbol': symbol,
                'side': pos.side,
                'entry': round(pos.entry_price, 2),
                'exit': round(exit_price, 2),
                'pnl': round(pos.pnl_percent, 2),
                'hold_minutes': round(hold_minutes, 1),
                'entry_time': datetime.fromtimestamp(pos.entry_time).strftime('%H:%M:%S'),
                'exit_time': datetime.fromtimestamp(exit_time).strftime('%H:%M:%S')
            }
            self.trades_history.append(trade_info)
            
            # Відправляємо в Telegram
            self.send_trade_result(trade_info)
            
            # Видаляємо позицію
            del self.positions[symbol]
            
            return trade_info
        return None
    
    def open_position(self, symbol, side, price, current_time):
        """Відкриває нову позицію"""
        self.positions[symbol] = Position(symbol, side, price, current_time)
        
        # Сповіщення про відкриття
        msg = (f"🆓 *НОВА ПОЗИЦІЯ*\n"
               f"Монета: {symbol}\n"
               f"Напрямок: {'🟢 LONG' if side == 'LONG' else '🔴 SHORT'}\n"
               f"Ціна входу: ${round(price, 2)}\n"
               f"Час: {datetime.fromtimestamp(current_time).strftime('%H:%M:%S')}")
        bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')
    
    def send_trade_result(self, trade):
        """Відправляє результат угоди"""
        emoji = '✅' if trade['pnl'] > 0 else '❌'
        msg = (f"{emoji} *РЕЗУЛЬТАТ УГОДИ*\n"
               f"Монета: {trade['symbol']}\n"
               f"Тип: {'🟢 LONG' if trade['side'] == 'LONG' else '🔴 SHORT'}\n"
               f"Вхід: ${trade['entry']} → Вихід: ${trade['exit']}\n"
               f"📊 PnL: *{trade['pnl']:+.2f}%*\n"
               f"⏱ Час утримання: {trade['hold_minutes']} хв\n"
               f"🕒 {trade['entry_time']} → {trade['exit_time']}")
        bot.send_message(config.CHAT_ID, msg, parse_mode='Markdown')
    
    def monitor_loop(self):
        """Головний цикл моніторингу"""
        print("🤖 Моніторинг запущено. Чекаємо на перетин EMA...")
        
        while self.running:
            current_time = time.time()
            
            for symbol in config.SYMBOLS:
                try:
                    signal, state, price = self.check_crossover(symbol)
                    
                    if signal:
                        print(f"🔥 {symbol}: СИГНАЛ {signal} (ціна: {price})")
                        
                        # Якщо є відкрита позиція для цієї пари - закриваємо
                        if symbol in self.positions:
                            current_pos = self.positions[symbol]
                            
                            # Закриваємо ТІЛЬКИ якщо сигнал протилежний
                            if (current_pos.side == 'LONG' and signal == 'SHORT') or \
                               (current_pos.side == 'SHORT' and signal == 'LONG'):
                                self.close_position(symbol, price, current_time)
                                
                                # Відкриваємо нову позицію (протилежну)
                                self.open_position(symbol, signal, price, current_time)
                            else:
                                print(f"⚠️ {symbol}: ігноруємо {signal} - вже є {current_pos.side}")
                        
                        else:
                            # Немає позиції - відкриваємо нову
                            self.open_position(symbol, signal, price, current_time)
                    
                except Exception as e:
                    print(f"Помилка для {symbol}: {e}")
            
            time.sleep(5)  # Перевірка кожні 5 секунд

# ===== КОМАНДИ TELEGRAM =====
@bot.message_handler(commands=['start'])
def start_cmd(message):
    bot.reply_to(message, "🚀 Бот запущено! Чекаємо на перетин EMA 12/26...")
    scalper = ScalperBot()
    thread = threading.Thread(target=scalper.monitor_loop, daemon=True)
    thread.start()
    bot.scalper = scalper

@bot.message_handler(commands=['stop'])
def stop_cmd(message):
    if hasattr(bot, 'scalper'):
        bot.scalper.running = False
        bot.reply_to(message, "⏹ Бот зупинено")
    else:
        bot.reply_to(message, "Бот не запущено")

@bot.message_handler(commands=['price'])
def price_cmd(message):
    try:
        msg = "💰 *Поточні ціни (KuCoin):*\n"
        for symbol in config.SYMBOLS:
            kucoin_symbol = symbol.replace('USDT', '-USDT')
            ticker = client.get_ticker(kucoin_symbol)
            price = float(ticker['price'])
            msg += f"\n{symbol}: ${round(price, 2)}"
        bot.reply_to(message, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Помилка: {e}")

@bot.message_handler(commands=['status'])
def status_cmd(message):
    if hasattr(bot, 'scalper') and bot.scalper.positions:
        msg = "📊 *Активні позиції:*\n"
        for symbol, pos in bot.scalper.positions.items():
            _, _, current_price = bot.scalper.get_emas(symbol)
            if pos.side == 'LONG':
                pnl = ((current_price - pos.entry_price) / pos.entry_price) * 100
            else:
                pnl = ((pos.entry_price - current_price) / pos.entry_price) * 100
            hold_time = (time.time() - pos.entry_time) / 60
            msg += (f"\n{symbol}: {'🟢 LONG' if pos.side == 'LONG' else '🔴 SHORT'}\n"
                    f"Вхід: ${round(pos.entry_price, 2)}\n"
                    f"PnL: {pnl:+.2f}% | {round(hold_time, 1)} хв\n")
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Немає активних позицій")

@bot.message_handler(commands=['history'])
def history_cmd(message):
    if hasattr(bot, 'scalper') and bot.scalper.trades_history:
        msg = "📜 *Останні 5 угод:*\n"
        for trade in bot.scalper.trades_history[-5:]:
            emoji = '✅' if trade['pnl'] > 0 else '❌'
            msg += f"\n{emoji} {trade['symbol']} {trade['side']}: {trade['pnl']:+.2f}%"
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Історія порожня")

@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    if hasattr(bot, 'scalper') and bot.scalper.trades_history:
        # Статистика по кожній монеті
        stats = {}
        for trade in bot.scalper.trades_history:
            symbol = trade['symbol']
            if symbol not in stats:
                stats[symbol] = {
                    'trades': 0,
                    'wins': 0,
                    'losses': 0,
                    'total_pnl': 0,
                    'longs': 0,
                    'shorts': 0
                }
            
            stats[symbol]['trades'] += 1
            stats[symbol]['total_pnl'] += trade['pnl']
            
            if trade['pnl'] > 0:
                stats[symbol]['wins'] += 1
            else:
                stats[symbol]['losses'] += 1
            
            if trade['side'] == 'LONG':
                stats[symbol]['longs'] += 1
            else:
                stats[symbol]['shorts'] += 1
        
        # Формуємо повідомлення
        msg = "📊 *ЗАГАЛЬНА СТАТИСТИКА*\n\n"
        
        for symbol, data in stats.items():
            winrate = (data['wins'] / data['trades'] * 100) if data['trades'] > 0 else 0
            msg += (f"*{symbol}*\n"
                   f"📈 Угод: {data['trades']}\n"
                   f"✅ Прибуткових: {data['wins']}\n"
                   f"❌ Збиткових: {data['losses']}\n"
                   f"🎯 Вінрейт: {winrate:.1f}%\n"
                   f"💰 Загальний PnL: {data['total_pnl']:+.2f}%\n"
                   f"🟢 LONG: {data['longs']} | 🔴 SHORT: {data['shorts']}\n\n")
        
        # Загальний підсумок
        total_trades = sum(d['trades'] for d in stats.values())
        total_pnl = sum(d['total_pnl'] for d in stats.values())
        msg += f"*ВСЬОГО*\n📊 Угод: {total_trades} | 💰 PnL: {total_pnl:+.2f}%"
        
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "Історія угод порожня")

@bot.message_handler(commands=['menu'])
def menu_cmd(message):
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn1 = types.KeyboardButton('/price')
    btn2 = types.KeyboardButton('/status')
    btn3 = types.KeyboardButton('/history')
    btn4 = types.KeyboardButton('/stats')
    btn5 = types.KeyboardButton('/start')
    btn6 = types.KeyboardButton('/stop')
    btn7 = types.KeyboardButton('/menu')
    markup.add(btn1, btn2, btn3, btn4, btn5, btn6, btn7)
    
    bot.send_message(message.chat.id, "📱 *Меню керування*\n\nВиберіть команду:", 
                    reply_markup=markup, parse_mode='Markdown')

# Обробка текстових команд з кнопок
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    text = message.text
    if text == '/price':
        price_cmd(message)
    elif text == '/status':
        status_cmd(message)
    elif text == '/history':
        history_cmd(message)
    elif text == '/stats':
        stats_cmd(message)
    elif text == '/start':
        start_cmd(message)
    elif text == '/stop':
        stop_cmd(message)
    elif text == '/menu':
        menu_cmd(message)

if __name__ == '__main__':
    print("🤖 Telegram Scalper Bot (KuCoin) запущено...")
    print(f"Моніторинг пар: {config.SYMBOLS}")
    print(f"EMA {config.EMA_FAST}/{config.EMA_SLOW} на {config.INTERVAL}")
    print("Команди: /menu - відкрити меню")
    bot.polling(none_stop=True)