import sqlite3
import pandas as pd
from datetime import datetime
import os

class TradeDatabase:
    def __init__(self, db_name='trades.db'):
        self.db_name = db_name
        self.conn = None
        self.connect()
        self.create_tables()
    
    def connect(self):
        self.conn = sqlite3.connect(self.db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
    
    def create_tables(self):
        cursor = self.conn.cursor()
        
        # Основна таблиця угод
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                entry_time DATETIME NOT NULL,
                exit_time DATETIME NOT NULL,
                hold_minutes REAL NOT NULL,
                pnl_percent REAL NOT NULL,
                real_pnl REAL NOT NULL, -- Чистий PnL за вирахуванням комісії
                max_pnl REAL,
                hour INTEGER,
                day_of_week INTEGER,
                exit_reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблиця станів бота (RSI зони: OVERSOLD, NORMAL, OVERBOUGHT)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_state (
                symbol TEXT PRIMARY KEY, -- Зробив PRIMARY KEY для INSERT OR REPLACE
                state TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Спрощена таблиця статистики (для швидкості роботи бота)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_trades INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                wins INTEGER DEFAULT 0
            )
        ''')
        
        self.conn.commit()

    # --- МЕТОДИ ДЛЯ СТАНІВ (RSI) ---
    def save_last_state(self, symbol, state):
        """Зберігає поточну зону RSI для монети"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO bot_state (symbol, state, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (symbol, state))
            self.conn.commit()
        except Exception as e:
            print(f"💾 Помилка збереження стану {symbol}: {e}")

    def load_last_state(self, symbol):
        """Завантажує зону RSI, щоб знати, чи ми вийшли з перепроданості/перекупленості"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT state FROM bot_state WHERE symbol = ?', (symbol,))
        row = cursor.fetchone()
        return row[0] if row else 'NORMAL'

    # --- МЕТОДИ ДЛЯ УГОД ---
    def add_trade(self, trade_info):
        """Додавання угоди з розрахунком часу та чистого профіту"""
        cursor = self.conn.cursor()
        now = datetime.now()
        
        # Розраховуємо чистий PnL (якщо бот передав вже готовий, беремо його)
        real_pnl = trade_info.get('real_pnl', trade_info['pnl'] - 0.2)

        cursor.execute('''
            INSERT INTO trades (
                symbol, side, entry_price, exit_price,
                entry_time, exit_time, hold_minutes,
                pnl_percent, real_pnl, max_pnl,
                hour, day_of_week, exit_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade_info['symbol'],
            trade_info['side'],
            trade_info['entry'],
            trade_info['exit'],
            trade_info['entry_time'],
            trade_info['exit_time'],
            trade_info['hold_minutes'],
            trade_info['pnl'],
            real_pnl,
            trade_info.get('max_pnl', trade_info['pnl']),
            now.hour,
            now.weekday(),
            trade_info.get('exit_reason', 'signal')
        ))
        self.conn.commit()

    def get_trades(self, limit=10):
        """Для команди /history"""
        query = f"SELECT * FROM trades ORDER BY exit_time DESC LIMIT {limit}"
        return pd.read_sql(query, self.conn)

    def get_detailed_analysis(self):
        """Для команди /stats"""
        df = pd.read_sql("SELECT * FROM trades", self.conn)
        if df.empty:
            return None
        
        analysis = {
            'total_trades': len(df),
            'wins': len(df[df['real_pnl'] > 0]),
            'losses': len(df[df['real_pnl'] <= 0]),
            'total_pnl': df['real_pnl'].sum(),
            'winrate': (len(df[df['real_pnl'] > 0]) / len(df)) * 100
        }
        return analysis

    def close(self):
        if self.conn:
            self.conn.close()

# Створюємо екземпляр
db = TradeDatabase()