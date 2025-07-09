-- Fog Controller Database Setup
-- Run this script to create the MySQL database and user

-- Create database
CREATE DATABASE IF NOT EXISTS fog_controller;

-- Create user (change password in production!)
CREATE USER IF NOT EXISTS 'fog_user'@'localhost' IDENTIFIED BY 'fog_password';

-- Grant privileges
GRANT ALL PRIVILEGES ON fog_controller.* TO 'fog_user'@'localhost';
FLUSH PRIVILEGES;

-- Use the database
USE fog_controller;

-- Create the activations table
CREATE TABLE IF NOT EXISTS fog_activations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    type ENUM('manual', 'auto') DEFAULT 'manual',
    duration INT DEFAULT 0,
    INDEX idx_timestamp (timestamp)
);

-- Insert some sample data for testing (optional)
-- INSERT INTO fog_activations (type, timestamp) VALUES 
-- ('manual', '2025-01-09 10:30:00'),
-- ('auto', '2025-01-09 11:00:00'),
-- ('manual', '2025-01-09 11:30:00');

SELECT 'Database setup completed successfully!' as message;