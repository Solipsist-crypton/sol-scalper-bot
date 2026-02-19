import sqlite3
import pandas as pd
from datetime import datetime

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
                max_pnl REAL,
                hour INTEGER,
                day_of_week INTEGER,
                day_of_month INTEGER,
                month INTEGER,
                year INTEGER,
                week_number INTEGER,
                exit_reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 🆕 НОВА ТАБЛИЦЯ ДЛЯ ЗБЕРЕЖЕННЯ СТАНІВ EMA
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
                achieved_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
        
        # Таблиця рекордів
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
                achieved_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.conn.commit()
    
    # 🆕 НОВІ МЕТОДИ ДЛЯ РОБОТИ ЗІ СТАНАМИ
    def save_last_state(self, symbol, state):
        """Зберігає стан EMA для символу"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO bot_state (symbol, state, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (symbol, state))
        self.conn.commit()
        print(f"💾 Стан {symbol} = {state} збережено в БД")
    
    def load_last_state(self, symbol):
        """Завантажує стан EMA для символу"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT state FROM bot_state WHERE symbol = ?', (symbol,))
        row = cursor.fetchone()
        return row[0] if row else None
    
    def add_trade(self, trade_info):
        """Додавання угоди в БД"""
        cursor = self.conn.cursor()
        
        cursor.execute('''
            INSERT INTO trades (
                symbol, side, entry_price, exit_price,
                entry_time, exit_time, hold_minutes,
                pnl_percent, max_pnl,
                hour, day_of_week, day_of_month, month, year, week_number,
                exit_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade_info['symbol'],
            trade_info['side'],
            trade_info['entry'],
            trade_info['exit'],
            trade_info['entry_time'],
            trade_info['exit_time'],
            trade_info['hold_minutes'],
            trade_info['pnl'],
            trade_info.get('max_pnl', trade_info['pnl']),
            datetime.now().hour,
            datetime.now().weekday(),
            datetime.now().day,
            datetime.now().month,
            datetime.now().year,
            datetime.now().isocalendar()[1],
            trade_info.get('exit_reason', 'signal')
        ))
        
        self.conn.commit()
        self.check_max_profit(trade_info)
        self.update_all_stats()
    
    def check_max_profit(self, trade_info):
        """Перевіряє чи є угода максимумом профіту"""
        cursor = self.conn.cursor()
        
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
    
    def update_all_stats(self):
        """Оновлення всіх статистик"""
        self.update_daily_stats()
        self.update_hourly_stats()
        self.update_weekly_stats()
        self.update_monthly_stats()
    
    def update_daily_stats(self):
        """Оновлення денної статистики"""
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
        """Оновлення годинної статистики"""
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
        """Оновлення тижневої статистики"""
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
        """Оновлення місячної статистики"""
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
    
    def get_max_profits(self, limit=10):
        """Отримати топ максимумів профіту"""
        return pd.read_sql(f'''
            SELECT * FROM max_profits 
            ORDER BY pnl_percent DESC LIMIT {limit}
        ''', self.conn)
    
    def get_max_losses(self, limit=10):
        """Отримати топ збитків"""
        return pd.read_sql(f'''
            SELECT * FROM max_profits 
            ORDER BY pnl_percent ASC LIMIT {limit}
        ''', self.conn)
    
    def get_records(self):
        """Отримати рекорди"""
        return pd.read_sql('''
            SELECT * FROM records 
            ORDER BY record_type, value DESC
        ''', self.conn)
    
    def get_trades(self, limit=100):
        """Отримати список угод"""
        return pd.read_sql(f'''
            SELECT * FROM trades 
            ORDER BY exit_time DESC LIMIT {limit}
        ''', self.conn)
    
    def get_daily_stats(self, days=30):
        """Отримати денну статистику"""
        return pd.read_sql(f'''
            SELECT * FROM daily_stats 
            ORDER BY date DESC LIMIT {days}
        ''', self.conn)
    
    def get_hourly_stats(self):
        """Отримати годинну статистику"""
        return pd.read_sql('''
            SELECT * FROM hourly_stats 
            ORDER BY hour
        ''', self.conn)
    
    def get_weekly_stats(self, weeks=12):
        """Отримати тижневу статистику"""
        return pd.read_sql(f'''
            SELECT * FROM weekly_stats 
            ORDER BY year DESC, week DESC LIMIT {weeks}
        ''', self.conn)
    
    def get_monthly_stats(self, months=12):
        """Отримати місячну статистику"""
        return pd.read_sql(f'''
            SELECT * FROM monthly_stats 
            ORDER BY year DESC, month DESC LIMIT {months}
        ''', self.conn)
    
    def get_detailed_analysis(self):
        """Детальний аналіз"""
        trades_df = pd.read_sql("SELECT * FROM trades", self.conn)
        
        if len(trades_df) == 0:
            return None
        
        # Отримуємо рекорди
        records = self.get_records()
        
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
        """Очистити всі дані"""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM trades")
        cursor.execute("DELETE FROM max_profits")
        cursor.execute("DELETE FROM records")
        cursor.execute("DELETE FROM daily_stats")
        cursor.execute("DELETE FROM hourly_stats")
        cursor.execute("DELETE FROM weekly_stats")
        cursor.execute("DELETE FROM monthly_stats")
        cursor.execute("DELETE FROM bot_state")  # Очищаємо також стани
        self.conn.commit()
        print("🗑️ Базу даних очищено")
    
    def close(self):
        """Закриття з'єднання"""
        if self.conn:
            self.conn.close()

# Глобальний екземпляр БД
db = TradeDatabase()