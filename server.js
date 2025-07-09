const express = require('express');
const cors = require('cors');
const path = require('path');
const fs = require('fs');
const { exec } = require('child_process');

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

// Middleware
app.use(express.json());

// Execute Python script command
function executeCommand(command) {
    return new Promise((resolve, reject) => {
        const pythonPath = config.pythonScriptPath;
        const scriptCommand = `sudo python3 ${pythonPath} --command ${command}`;
        
        console.log(`Executing: ${scriptCommand}`);
        
        exec(scriptCommand, (error, stdout, stderr) => {
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
app.get('/api/status', (req, res) => {
    res.json({
        fogActive: config.fogActive,
        lastCommand: config.lastCommand,
        lastActivated: config.lastActivated,
        activationCount: config.activationCount,
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
        const pythonPath = config.pythonScriptPath;
        const scriptCommand = `sudo python3 ${pythonPath} --command custom --code ${code}`;
        
        exec(scriptCommand, (error, stdout, stderr) => {
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

// Start server
app.listen(PORT, () => {
    console.log(`\n🌫️  Fog Controller Server`);
    console.log(`📡 Running on http://localhost:${PORT}`);
    console.log(`📊 Status: ${config.fogActive ? 'FOG ACTIVE' : 'Standby'}`);
    console.log(`📈 Total activations: ${config.activationCount}`);
    console.log(`\n✅ Server ready for connections!\n`);
});