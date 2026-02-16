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
        """Підключення до БД"""
        self.conn = sqlite3.connect(self.db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
    
    def create_tables(self):
        """Створення таблиць"""
        cursor = self.conn.cursor()
        
        # Таблиця угод
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
                hour INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                day_of_month INTEGER NOT NULL,
                month INTEGER NOT NULL,
                year INTEGER NOT NULL,
                week_number INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблиця максимумів профіту
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS max_profits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                pnl_percent REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                entry_time DATETIME NOT NULL,
                exit_time DATETIME NOT NULL,
                hold_minutes REAL NOT NULL,
                achieved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, pnl_percent, entry_time)
            )
        ''')
        
        # Таблиця денної статистики
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                total_trades INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                best_trade REAL DEFAULT 0,
                worst_trade REAL DEFAULT 0,
                avg_pnl REAL DEFAULT 0,
                avg_hold_minutes REAL DEFAULT 0,
                max_profit REAL DEFAULT 0,
                max_loss REAL DEFAULT 0,
                UNIQUE(date, symbol)
            )
        ''')
        
        # Таблиця годинної статистики
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hourly_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hour INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                total_trades INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                winrate REAL DEFAULT 0,
                avg_pnl REAL DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                max_profit REAL DEFAULT 0,
                max_loss REAL DEFAULT 0,
                UNIQUE(hour, symbol)
            )
        ''')
        
        # Таблиця тижневої статистики
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS weekly_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                week INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                total_trades INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                avg_pnl REAL DEFAULT 0,
                max_profit REAL DEFAULT 0,
                max_loss REAL DEFAULT 0,
                UNIQUE(year, week, symbol)
            )
        ''')
        
        # Таблиця місячної статистики
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS monthly_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                total_trades INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                avg_pnl REAL DEFAULT 0,
                max_profit REAL DEFAULT 0,
                max_loss REAL DEFAULT 0,
                UNIQUE(year, month, symbol)
            )
        ''')
        
        # Таблиця рекордів (топ-10)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                value REAL NOT NULL,
                side TEXT,
                entry_price REAL,
                exit_price REAL,
                entry_time DATETIME,
                exit_time DATETIME,
                achieved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(record_type, symbol, value)
            )
        ''')
        
        self.conn.commit()
    
    def add_trade(self, trade_info):
        """Додавання угоди в БД"""
        cursor = self.conn.cursor()
        
        # Парсимо час
        exit_dt = datetime.strptime(trade_info['exit_time'], '%H:%M:%S')
        entry_dt = datetime.strptime(trade_info['entry_time'], '%H:%M:%S')
        now = datetime.now()
        
        # Комбінуємо з сьогоднішньою датою
        exit_full = now.replace(hour=exit_dt.hour, minute=exit_dt.minute, second=exit_dt.second)
        entry_full = now.replace(hour=entry_dt.hour, minute=entry_dt.minute, second=entry_dt.second)
        
        cursor.execute('''
            INSERT INTO trades (
                symbol, side, entry_price, exit_price,
                entry_time, exit_time, hold_minutes,
                pnl_percent,
                hour, day_of_week, day_of_month, month, year, week_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade_info['symbol'],
            trade_info['side'],
            trade_info['entry'],
            trade_info['exit'],
            entry_full.strftime('%Y-%m-%d %H:%M:%S'),
            exit_full.strftime('%Y-%m-%d %H:%M:%S'),
            trade_info['hold_minutes'],
            trade_info['pnl'],
            exit_dt.hour,
            exit_dt.weekday(),
            exit_dt.day,
            exit_dt.month,
            exit_dt.year,
            exit_dt.isocalendar()[1]
        ))
        
        self.conn.commit()
        
        # Перевіряємо чи це новий максимум/рекорд
        self.check_max_profit(trade_info)
        self.check_records(trade_info)
        self.update_all_stats()
    
    def check_max_profit(self, trade_info):
        """Перевіряє чи є угода максимумом профіту"""
        cursor = self.conn.cursor()
        
        # Шукаємо існуючі максимуми для цього символу
        cursor.execute('''
            SELECT pnl_percent FROM max_profits 
            WHERE symbol = ? 
            ORDER BY pnl_percent DESC LIMIT 5
        ''', (trade_info['symbol'],))
        
        top_profits = [row[0] for row in cursor.fetchall()]
        
        # Якщо це в топ-5 або це найбільший прибуток/збиток
        if len(top_profits) < 5 or trade_info['pnl'] > min(top_profits) or trade_info['pnl'] < -5:
            cursor.execute('''
                INSERT OR IGNORE INTO max_profits 
                (symbol, side, pnl_percent, entry_price, exit_price, entry_time, exit_time, hold_minutes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trade_info['symbol'],
                trade_info['side'],
                trade_info['pnl'],
                trade_info['entry'],
                trade_info['exit'],
                trade_info['entry_time'],
                trade_info['exit_time'],
                trade_info['hold_minutes']
            ))
            self.conn.commit()
    
    def check_records(self, trade_info):
        """Перевіряє рекорди"""
        cursor = self.conn.cursor()
        
        # Найбільший прибуток
        cursor.execute('''
            INSERT OR REPLACE INTO records (record_type, symbol, value, side, entry_price, exit_price, entry_time, exit_time)
            SELECT 'MAX_PROFIT', ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM records 
                WHERE record_type = 'MAX_PROFIT' AND symbol = ? AND value >= ?
            ) OR ? > (SELECT value FROM records WHERE record_type = 'MAX_PROFIT' AND symbol = ?)
        ''', (
            trade_info['symbol'], trade_info['pnl'], trade_info['side'],
            trade_info['entry'], trade_info['exit'],
            trade_info['entry_time'], trade_info['exit_time'],
            trade_info['symbol'], trade_info['pnl'],
            trade_info['pnl'], trade_info['symbol']
        ))
        
        # Найбільший збиток
        cursor.execute('''
            INSERT OR REPLACE INTO records (record_type, symbol, value, side, entry_price, exit_price, entry_time, exit_time)
            SELECT 'MAX_LOSS', ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM records 
                WHERE record_type = 'MAX_LOSS' AND symbol = ? AND value <= ?
            ) OR ? < (SELECT value FROM records WHERE record_type = 'MAX_LOSS' AND symbol = ?)
        ''', (
            trade_info['symbol'], trade_info['pnl'], trade_info['side'],
            trade_info['entry'], trade_info['exit'],
            trade_info['entry_time'], trade_info['exit_time'],
            trade_info['symbol'], trade_info['pnl'],
            trade_info['pnl'], trade_info['symbol']
        ))
        
        self.conn.commit()
    
    def update_all_stats(self):
        """Оновлення всіх статистик"""
        self.update_daily_stats()
        self.update_hourly_stats()
        self.update_weekly_stats()
        self.update_monthly_stats()
    
    def update_daily_stats(self):
        """Оновлення денної статистики з максимумами"""
        cursor = self.conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO daily_stats (
                date, symbol, total_trades, wins, losses, total_pnl,
                best_trade, worst_trade, avg_pnl, avg_hold_minutes,
                max_profit, max_loss
            )
            SELECT 
                date(exit_time) as date,
                symbol,
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl_percent > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_percent < 0 THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(pnl_percent), 2) as total_pnl,
                ROUND(MAX(pnl_percent), 2) as best_trade,
                ROUND(MIN(pnl_percent), 2) as worst_trade,
                ROUND(AVG(pnl_percent), 2) as avg_pnl,
                ROUND(AVG(hold_minutes), 1) as avg_hold_minutes,
                ROUND(MAX(CASE WHEN pnl_percent > 0 THEN pnl_percent ELSE 0 END), 2) as max_profit,
                ROUND(MIN(CASE WHEN pnl_percent < 0 THEN pnl_percent ELSE 0 END), 2) as max_loss
            FROM trades
            GROUP BY date(exit_time), symbol
        ''')
        
        self.conn.commit()
    
    def update_hourly_stats(self):
        """Оновлення годинної статистики з максимумами"""
        cursor = self.conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO hourly_stats (
                hour, symbol, total_trades, wins, losses, winrate, 
                avg_pnl, total_pnl, max_profit, max_loss
            )
            SELECT 
                hour,
                symbol,
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl_percent > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_percent < 0 THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(CASE WHEN pnl_percent > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) as winrate,
                ROUND(AVG(pnl_percent), 2) as avg_pnl,
                ROUND(SUM(pnl_percent), 2) as total_pnl,
                ROUND(MAX(pnl_percent), 2) as max_profit,
                ROUND(MIN(pnl_percent), 2) as max_loss
            FROM trades
            GROUP BY hour, symbol
        ''')
        
        self.conn.commit()
    
    def update_weekly_stats(self):
        """Оновлення тижневої статистики з максимумами"""
        cursor = self.conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO weekly_stats (
                year, week, symbol, total_trades, wins, losses, 
                total_pnl, avg_pnl, max_profit, max_loss
            )
            SELECT 
                year,
                week_number,
                symbol,
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl_percent > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_percent < 0 THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(pnl_percent), 2) as total_pnl,
                ROUND(AVG(pnl_percent), 2) as avg_pnl,
                ROUND(MAX(pnl_percent), 2) as max_profit,
                ROUND(MIN(pnl_percent), 2) as max_loss
            FROM trades
            GROUP BY year, week_number, symbol
        ''')
        
        self.conn.commit()
    
    def update_monthly_stats(self):
        """Оновлення місячної статистики з максимумами"""
        cursor = self.conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO monthly_stats (
                year, month, symbol, total_trades, wins, losses, 
                total_pnl, avg_pnl, max_profit, max_loss
            )
            SELECT 
                year,
                month,
                symbol,
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl_percent > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_percent < 0 THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(pnl_percent), 2) as total_pnl,
                ROUND(AVG(pnl_percent), 2) as avg_pnl,
                ROUND(MAX(pnl_percent), 2) as max_profit,
                ROUND(MIN(pnl_percent), 2) as max_loss
            FROM trades
            GROUP BY year, month, symbol
        ''')
        
        self.conn.commit()
    
    def get_max_profits(self, symbol=None, limit=10):
        """Отримати топ максимумів профіту"""
        if symbol:
            query = '''
                SELECT * FROM max_profits 
                WHERE symbol = ? 
                ORDER BY pnl_percent DESC LIMIT ?
            '''
            return pd.read_sql(query, self.conn, params=(symbol, limit))
        else:
            return pd.read_sql(f'''
                SELECT * FROM max_profits 
                ORDER BY pnl_percent DESC LIMIT {limit}
            ''', self.conn)
    
    def get_max_losses(self, symbol=None, limit=10):
        """Отримати топ збитків"""
        if symbol:
            query = '''
                SELECT * FROM max_profits 
                WHERE symbol = ? 
                ORDER BY pnl_percent ASC LIMIT ?
            '''
            return pd.read_sql(query, self.conn, params=(symbol, limit))
        else:
            return pd.read_sql(f'''
                SELECT * FROM max_profits 
                ORDER BY pnl_percent ASC LIMIT {limit}
            ''', self.conn)
    
    def get_records(self, symbol=None):
        """Отримати рекорди"""
        if symbol:
            query = "SELECT * FROM records WHERE symbol = ? ORDER BY record_type"
            return pd.read_sql(query, self.conn, params=(symbol,))
        else:
            return pd.read_sql("SELECT * FROM records ORDER BY record_type", self.conn)
    
    def get_trades(self, symbol=None, limit=100):
        """Отримати список угод"""
        if symbol:
            query = "SELECT * FROM trades WHERE symbol = ? ORDER BY exit_time DESC LIMIT ?"
            return pd.read_sql(query, self.conn, params=(symbol, limit))
        else:
            return pd.read_sql(f"SELECT * FROM trades ORDER BY exit_time DESC LIMIT {limit}", self.conn)
    
    def get_daily_stats(self, symbol=None, days=30):
        """Отримати денну статистику"""
        if symbol:
            query = "SELECT * FROM daily_stats WHERE symbol = ? ORDER BY date DESC LIMIT ?"
            return pd.read_sql(query, self.conn, params=(symbol, days))
        else:
            return pd.read_sql(f"SELECT * FROM daily_stats ORDER BY date DESC LIMIT {days}", self.conn)
    
    def get_hourly_stats(self, symbol=None):
        """Отримати годинну статистику"""
        if symbol:
            query = "SELECT * FROM hourly_stats WHERE symbol = ? ORDER BY hour"
            return pd.read_sql(query, self.conn, params=(symbol,))
        else:
            return pd.read_sql("SELECT * FROM hourly_stats ORDER BY hour", self.conn)
    
    def get_weekly_stats(self, symbol=None, weeks=12):
        """Отримати тижневу статистику"""
        if symbol:
            query = "SELECT * FROM weekly_stats WHERE symbol = ? ORDER BY year DESC, week DESC LIMIT ?"
            return pd.read_sql(query, self.conn, params=(symbol, weeks))
        else:
            return pd.read_sql(f"SELECT * FROM weekly_stats ORDER BY year DESC, week DESC LIMIT {weeks}", self.conn)
    
    def get_monthly_stats(self, symbol=None, months=12):
        """Отримати місячну статистику"""
        if symbol:
            query = "SELECT * FROM monthly_stats WHERE symbol = ? ORDER BY year DESC, month DESC LIMIT ?"
            return pd.read_sql(query, self.conn, params=(symbol, months))
        else:
            return pd.read_sql(f"SELECT * FROM monthly_stats ORDER BY year DESC, month DESC LIMIT {months}", self.conn)
    
    def get_detailed_analysis(self, symbol=None):
        """Детальний аналіз з максимумами"""
        if symbol:
            trades_df = pd.read_sql("SELECT * FROM trades WHERE symbol = ?", self.conn, params=(symbol,))
        else:
            trades_df = pd.read_sql("SELECT * FROM trades", self.conn)
        
        if len(trades_df) == 0:
            return None
        
        # Отримуємо рекорди
        records = self.get_records(symbol)
        
        analysis = {
            'total_trades': len(trades_df),
            'wins': (trades_df['pnl_percent'] > 0).sum(),
            'losses': (trades_df['pnl_percent'] < 0).sum(),
            'total_pnl': trades_df['pnl_percent'].sum(),
            'avg_pnl': trades_df['pnl_percent'].mean(),
            'best_trade': trades_df['pnl_percent'].max(),
            'worst_trade': trades_df['pnl_percent'].min(),
            'avg_hold': trades_df['hold_minutes'].mean(),
            'winrate': (trades_df['pnl_percent'] > 0).mean() * 100,
            'profit_factor': abs(trades_df[trades_df['pnl_percent'] > 0]['pnl_percent'].sum() / 
                                trades_df[trades_df['pnl_percent'] < 0]['pnl_percent'].sum()) if len(trades_df[trades_df['pnl_percent'] < 0]) > 0 else float('inf'),
            'records': records.to_dict('records') if len(records) > 0 else [],
            'by_hour': trades_df.groupby('hour').agg({
                'pnl_percent': ['count', 'mean', 'max', 'min', lambda x: (x > 0).mean() * 100]
            }).round(2),
            'by_day': trades_df.groupby('day_of_week').agg({
                'pnl_percent': ['count', 'mean', 'max', 'min', lambda x: (x > 0).mean() * 100]
            }).round(2)
        }
        
        return analysis
    
    def clear_all_data(self):
        """Очистити всі дані (тільки для тестування)"""
        cursor = self.conn.cursor()
    
        # Видаляємо всі дані з таблиць
        cursor.execute("DELETE FROM trades")
        cursor.execute("DELETE FROM max_profits")
        cursor.execute("DELETE FROM records")
        cursor.execute("DELETE FROM daily_stats")
        cursor.execute("DELETE FROM hourly_stats")
        cursor.execute("DELETE FROM weekly_stats")
        cursor.execute("DELETE FROM monthly_stats")
    
        self.conn.commit()
        print("🗑️ Базу даних очищено")


    def close(self):
        """Закриття з'єднання"""
        if self.conn:
            self.conn.close()

# Глобальний екземпляр БД
db = TradeDatabase()