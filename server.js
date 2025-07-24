const express = require('express');
const cors = require('cors');
const path = require('path');
const fs = require('fs');
const { exec, execFile } = require('child_process');
const mysql = require('mysql2/promise');
const cron = require('node-cron');

const app = express();
const PORT = process.env.PORT || 5003;

// Enable CORS
app.use(cors());

// Serve static files with proper headers for PWA
app.use(express.static(path.join(__dirname, 'public'), {
    setHeaders: (res, path) => {
        // Set appropriate cache headers for PWA files
        if (path.endsWith('manifest.json')) {
            res.setHeader('Content-Type', 'application/manifest+json');
            res.setHeader('Cache-Control', 'no-cache');
        }
        if (path.endsWith('browserconfig.xml')) {
            res.setHeader('Content-Type', 'application/xml');
            res.setHeader('Cache-Control', 'no-cache');
        }
        // Cache static assets for PWA
        if (path.endsWith('.png') || path.endsWith('.ico') || path.endsWith('.jpg') || path.endsWith('.jpeg')) {
            res.setHeader('Cache-Control', 'public, max-age=31536000'); // 1 year
        }
        // Set proper MIME type for images
        if (path.endsWith('.jpg') || path.endsWith('.jpeg')) {
            res.setHeader('Content-Type', 'image/jpeg');
        }
    }
}));

// Config file path
const CONFIG_FILE = path.join(__dirname, 'fog-config.json');

// Load config from file
function loadConfig() {
    try {
        if (fs.existsSync(CONFIG_FILE)) {
            const config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
            return config;
        }
    } catch (error) {
        console.error('Error loading config:', error);
    }
    return {
        pythonScriptPath: path.join(__dirname, 'fog-controller.py'),
        lastCommand: null,
        fogActive: false,
        lastActivated: null,
        activationCount: 0
    };
}

// Save config to file
function saveConfig(config) {
    try {
        fs.writeFileSync(CONFIG_FILE, JSON.stringify(config, null, 2));
        console.log('Config saved');
    } catch (error) {
        console.error('Error saving config:', error);
    }
}

// Load initial config
let config = loadConfig();

// Auto-Fog state
let autoFogState = {
    active: false,
    interval: 5, // minutes
    cronJob: null,
    startTime: null,
    autoDisableTime: null,
    autoDisableTimeout: null
};

// MySQL connection
let db = null;

// Initialize MySQL connection
async function initializeDatabase() {
    try {
        db = await mysql.createConnection({
            host: process.env.DB_HOST || '127.0.0.1',
            user: process.env.DB_USER || 'fog_user',
            password: process.env.DB_PASSWORD || 'fog_password',
            database: process.env.DB_NAME || 'fog_controller'
        });
        
        // Create table if it doesn't exist
        await db.execute(`
            CREATE TABLE IF NOT EXISTS fog_activations (
                id INT AUTO_INCREMENT PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                type ENUM('manual', 'auto') DEFAULT 'manual',
                duration INT DEFAULT 0,
                INDEX idx_timestamp (timestamp)
            )
        `);
        
        console.log('📊 MySQL database connected successfully');
    } catch (error) {
        console.error('❌ MySQL connection failed:', error.message);
        console.log('⚠️  Running without database logging');
    }
}

// Log activation to database
async function logActivation(type = 'manual') {
    if (!db) return;
    
    try {
        await db.execute(
            'INSERT INTO fog_activations (type) VALUES (?)',
            [type]
        );
        console.log(`📝 Logged ${type} activation to database`);
    } catch (error) {
        console.error('❌ Failed to log activation:', error.message);
    }
}

// Get usage analytics
async function getUsageAnalytics() {
    if (!db) return { hourlyData: [], peakHour: null };
    
    try {
        // Get hourly usage data for the last 24 hours
        const [hourlyRows] = await db.execute(`
            SELECT 
                HOUR(timestamp) as hour,
                COUNT(*) as count
            FROM fog_activations 
            WHERE timestamp >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
            GROUP BY HOUR(timestamp)
            ORDER BY hour
        `);
        
        // Fill missing hours with 0
        const hourlyData = [];
        for (let i = 0; i < 24; i++) {
            const found = hourlyRows.find(row => row.hour === i);
            hourlyData.push({
                hour: i,
                count: found ? found.count : 0
            });
        }
        
        // Get peak hour
        const [peakRows] = await db.execute(`
            SELECT 
                HOUR(timestamp) as hour,
                COUNT(*) as count
            FROM fog_activations 
            WHERE timestamp >= DATE_SUB(NOW(), INTERVAL 7 DAY)
            GROUP BY HOUR(timestamp)
            ORDER BY count DESC
            LIMIT 1
        `);
        
        const peakHour = peakRows.length > 0 ? `${peakRows[0].hour}:00` : null;
        
        return { hourlyData, peakHour };
    } catch (error) {
        console.error('❌ Failed to get analytics:', error.message);
        return { hourlyData: [], peakHour: null };
    }
}

// Auto-Fog functions
function startAutoFog(interval) {
    // Check if already starting/active to prevent race condition
    if (autoFogState.active || autoFogState.cronJob) {
        console.log('⚠️  Auto-Fog already active or starting');
        return;
    }
    
    // Set active flag immediately to prevent concurrent starts
    autoFogState.active = true;
    
    try {
        const cronPattern = `*/${interval} * * * *`; // Every X minutes
        
        autoFogState.cronJob = cron.schedule(cronPattern, async () => {
            console.log(`🤖 Auto-Fog triggered (${interval} min interval)`);
            try {
                await executeCommand('on', 'auto');
            } catch (error) {
                console.error('❌ Auto-Fog execution failed:', error);
            }
        }, {
            scheduled: false
        });
        
        autoFogState.interval = interval;
        autoFogState.startTime = Date.now();
        autoFogState.autoDisableTime = Date.now() + (60 * 60 * 1000); // 1 hour from now
        autoFogState.cronJob.start();
        
        // Auto-disable after 1 hour
        autoFogState.autoDisableTimeout = setTimeout(() => {
            console.log('⏰ Auto-Fog automatically disabled after 1 hour');
            stopAutoFog();
        }, 60 * 60 * 1000); // 1 hour
        
        console.log(`✅ Auto-Fog started: ${interval} min interval, auto-disable in 1 hour`);
    } catch (error) {
        console.error('❌ Failed to start Auto-Fog:', error);
        autoFogState.active = false;
        throw error;
    }
}

function stopAutoFog() {
    if (autoFogState.cronJob) {
        autoFogState.cronJob.destroy();
        autoFogState.cronJob = null;
    }
    
    if (autoFogState.autoDisableTimeout) {
        clearTimeout(autoFogState.autoDisableTimeout);
        autoFogState.autoDisableTimeout = null;
    }
    
    autoFogState.active = false;
    autoFogState.startTime = null;
    autoFogState.autoDisableTime = null;
    
    console.log('🛑 Auto-Fog stopped');
}

// Initialize database on startup
initializeDatabase();

// Middleware
app.use(express.json());

// Execute Python script command
function executeCommand(command, type = 'manual') {
    return new Promise((resolve, reject) => {
        // Validate command input
        const validCommands = ['on', 'off'];
        if (!validCommands.includes(command)) {
            reject(new Error('Invalid command. Must be "on" or "off"'));
            return;
        }
        
        const pythonPath = path.join(__dirname, 'fog-controller.py');
        
        console.log(`Executing: sudo python3 ${pythonPath} --command ${command}`);
        
        // Use execFile for better security
        execFile('sudo', ['python3', pythonPath, '--command', command], async (error, stdout, stderr) => {
            if (error) {
                console.error(`Error: ${error}`);
                reject(error);
                return;
            }
            
            if (stderr) {
                console.error(`stderr: ${stderr}`);
            }
            
            console.log(`stdout: ${stdout}`);
            
            // Update config based on command
            config.lastCommand = command;
            if (command === 'on') {
                config.fogActive = true;
                config.lastActivated = new Date().toISOString();
                config.activationCount++;
                
                // Log to database
                await logActivation(type);
            } else if (command === 'off') {
                config.fogActive = false;
            }
            saveConfig(config);
            
            resolve(stdout);
        });
    });
}

// API endpoints

// Health check
app.get('/api/health', (req, res) => {
    res.json({ 
        status: 'ok', 
        service: 'Fog Controller',
        port: PORT,
        timestamp: new Date().toISOString()
    });
});

// Get current status
app.get('/api/status', async (req, res) => {
    const analytics = await getUsageAnalytics();
    res.json({
        fogActive: config.fogActive,
        lastCommand: config.lastCommand,
        lastActivated: config.lastActivated,
        activationCount: config.activationCount,
        peakHour: analytics.peakHour,
        timestamp: new Date().toISOString()
    });
});

// Turn fog ON
app.post('/api/fog/on', async (req, res) => {
    try {
        await executeCommand('on');
        res.json({ 
            success: true, 
            message: 'Fog machine turned ON',
            status: 'on'
        });
    } catch (error) {
        res.status(500).json({ 
            success: false, 
            error: 'Failed to turn on fog machine',
            details: error.message 
        });
    }
});

// Turn fog OFF
app.post('/api/fog/off', async (req, res) => {
    try {
        await executeCommand('off');
        res.json({ 
            success: true, 
            message: 'Fog machine turned OFF',
            status: 'off'
        });
    } catch (error) {
        res.status(500).json({ 
            success: false, 
            error: 'Failed to turn off fog machine',
            details: error.message 
        });
    }
});

// Toggle fog state
app.post('/api/fog/toggle', async (req, res) => {
    try {
        const newState = config.fogActive ? 'off' : 'on';
        await executeCommand(newState);
        res.json({ 
            success: true, 
            message: `Fog machine toggled to ${newState.toUpperCase()}`,
            status: newState
        });
    } catch (error) {
        res.status(500).json({ 
            success: false, 
            error: 'Failed to toggle fog machine',
            details: error.message 
        });
    }
});

// Reset statistics
app.post('/api/stats/reset', (req, res) => {
    config.activationCount = 0;
    config.lastActivated = null;
    saveConfig(config);
    res.json({ 
        success: true, 
        message: 'Statistics reset successfully'
    });
});

// Custom code endpoint (for testing)
app.post('/api/fog/custom', async (req, res) => {
    const { code } = req.body;
    if (!code) {
        return res.status(400).json({ 
            success: false, 
            error: 'Code parameter required' 
        });
    }
    
    try {
        // Validate custom code (only allow hex digits)
        if (!/^[0-9A-Fa-f]+$/.test(code)) {
            return res.status(400).json({ 
                success: false, 
                error: 'Invalid code format. Only hexadecimal characters allowed' 
            });
        }
        
        const pythonPath = path.join(__dirname, 'fog-controller.py');
        
        execFile('sudo', ['python3', pythonPath, '--command', 'custom', '--code', code], (error, stdout, stderr) => {
            if (error) {
                return res.status(500).json({ 
                    success: false, 
                    error: 'Failed to send custom code',
                    details: error.message 
                });
            }
            
            res.json({ 
                success: true, 
                message: 'Custom code sent successfully',
                code: code
            });
        });
    } catch (error) {
        res.status(500).json({ 
            success: false, 
            error: 'Failed to send custom code',
            details: error.message 
        });
    }
});

// Auto-Fog endpoints
app.get('/api/auto-fog/status', (req, res) => {
    res.json({
        active: autoFogState.active,
        interval: autoFogState.interval,
        startTime: autoFogState.startTime,
        autoDisableTime: autoFogState.autoDisableTime,
        timestamp: new Date().toISOString()
    });
});

app.post('/api/auto-fog/enable', (req, res) => {
    const { interval } = req.body;
    
    if (!interval || ![2, 5, 10].includes(parseInt(interval))) {
        return res.status(400).json({
            success: false,
            error: 'Invalid interval. Must be 2, 5, or 10 minutes'
        });
    }
    
    try {
        startAutoFog(parseInt(interval));
        res.json({
            success: true,
            message: `Auto-Fog enabled with ${interval} minute interval`,
            interval: parseInt(interval)
        });
    } catch (error) {
        res.status(500).json({
            success: false,
            error: 'Failed to enable Auto-Fog',
            details: error.message
        });
    }
});

app.post('/api/auto-fog/disable', (req, res) => {
    try {
        stopAutoFog();
        res.json({
            success: true,
            message: 'Auto-Fog disabled'
        });
    } catch (error) {
        res.status(500).json({
            success: false,
            error: 'Failed to disable Auto-Fog',
            details: error.message
        });
    }
});

// Analytics endpoints
app.get('/api/analytics/usage', async (req, res) => {
    try {
        const analytics = await getUsageAnalytics();
        res.json({
            success: true,
            ...analytics
        });
    } catch (error) {
        res.status(500).json({
            success: false,
            error: 'Failed to get analytics',
            details: error.message
        });
    }
});

// Graceful shutdown handling
process.on('SIGTERM', gracefulShutdown);
process.on('SIGINT', gracefulShutdown);

async function gracefulShutdown() {
    console.log('\n⚠️  Shutting down gracefully...');
    
    // Stop auto-fog if running
    if (autoFogState.active) {
        stopAutoFog();
    }
    
    // Close database connection
    if (db) {
        try {
            await db.end();
            console.log('📊 Database connection closed');
        } catch (error) {
            console.error('❌ Error closing database:', error.message);
        }
    }
    
    // Save final config state
    saveConfig(config);
    
    console.log('👋 Shutdown complete');
    process.exit(0);
}

// Start server
app.listen(PORT, () => {
    console.log(`\n💨 Fog Controller Server`);
    console.log(`📡 Running on http://localhost:${PORT}`);
    console.log(`📊 Status: ${config.fogActive ? 'FOG ACTIVE' : 'Standby'}`);
    console.log(`📈 Total activations: ${config.activationCount}`);
    console.log(`🤖 Auto-Fog: ${autoFogState.active ? `Active (${autoFogState.interval} min)` : 'Disabled'}`);
    console.log(`📊 Database: ${db ? 'Connected' : 'Not connected'}`);
    console.log(`\n✅ Server ready for connections!\n`);
});