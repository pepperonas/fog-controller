module.exports = {
  apps: [{
    name: 'fog-controller',
    script: './server.js',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '200M',
    env: {
      NODE_ENV: 'production',
      PORT: 5003
    },
    error_file: 'logs/err.log',
    out_file: 'logs/out.log',
    log_file: 'logs/combined.log',
    time: true,
    
    // Restart strategies
    min_uptime: '10s',
    max_restarts: 10,
    restart_delay: 4000,
    
    // Deployment environment
    env_production: {
      NODE_ENV: 'production',
      PORT: 5003
    },
    
    // Post-start check
    wait_ready: true,
    listen_timeout: 3000
  }]
};