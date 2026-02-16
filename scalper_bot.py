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