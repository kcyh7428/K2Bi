// K2Bi pm2 ecosystem -- Mac Mini Trader tier daemons.
//
// Phase 2 scaffold. Stub entries only; implementations land per
// milestone in the Phase 2 build sessions. Until each implementation
// ships, the corresponding pm2 entry stays commented out so the Mini
// does not try to boot a non-existent script.
//
// pm2 start ecosystem.config.js --only invest-execute   (once 2.6 ships)
// pm2 start ecosystem.config.js --only invest-alert     (once 2.9 ships)
// pm2 start ecosystem.config.js --only invest-feed      (once 2.10 ships)
// pm2 start ecosystem.config.js --only invest-observer-loop  (Phase 4+)

module.exports = {
  apps: [
    // {
    //   name: 'invest-execute',
    //   script: 'execution/engine/main.py',
    //   interpreter: 'python3',
    //   cwd: '/Users/fastshower/Projects/K2Bi',
    //   autorestart: false,       // cron-triggered, not continuous
    //   // Three pm2 entries are needed to cover the 09:30-16:00 ET window
    //   // exactly (cron cannot express "start at 30-past in hour N, every 5
    //   // min, stop at top of hour N+M" in a single expression). The engine's
    //   // market_hours validator is the hard enforcer; cron width is a
    //   // performance concern only, not a safety one. See the -open and
    //   // -close entries below.
    //   cron_restart: '*/5 10-15 * * 1-5',  // main window: every 5 min, 10:00-15:55 ET, Mon-Fri
    //   max_memory_restart: '512M',
    //   env: {
    //     NODE_ENV: 'paper',
    //     IBKR_HOST: '127.0.0.1',
    //     IBKR_PORT: '4002',
    //     IBKR_CLIENT_ID: '7428',
    //   },
    //   error_file: '/Users/fastshower/Projects/K2Bi/logs/invest-execute.err.log',
    //   out_file:   '/Users/fastshower/Projects/K2Bi/logs/invest-execute.out.log',
    // },
    // {
    //   name: 'invest-execute-open',   // 09:30-09:55 ET ticks (6 firings)
    //   script: 'execution/engine/main.py',
    //   interpreter: 'python3',
    //   cwd: '/Users/fastshower/Projects/K2Bi',
    //   autorestart: false,
    //   cron_restart: '30-55/5 9 * * 1-5',
    // },
    // {
    //   name: 'invest-execute-close',  // 16:00 ET tick (final bar)
    //   script: 'execution/engine/main.py',
    //   interpreter: 'python3',
    //   cwd: '/Users/fastshower/Projects/K2Bi',
    //   autorestart: false,
    //   cron_restart: '0 16 * * 1-5',
    // },
    // {
    //   name: 'invest-alert',
    //   script: 'execution/alert/bot.py',
    //   interpreter: 'python3',
    //   cwd: '/Users/fastshower/Projects/K2Bi',
    //   autorestart: true,
    //   max_memory_restart: '256M',
    //   env: {
    //     NODE_ENV: 'paper',
    //     TELEGRAM_BOT_TOKEN: '__from_env__',
    //     TELEGRAM_CHAT_ID: '__from_env__',
    //   },
    // },
    // {
    //   name: 'invest-feed',
    //   script: 'scripts/invest-feed.py',
    //   interpreter: 'python3',
    //   cwd: '/Users/fastshower/Projects/K2Bi',
    //   autorestart: false,
    //   // Same market-hours window pattern as invest-execute. Engine's
    //   // market_hours validator is not applicable here (feed writes to raw/
    //   // only, doesn't submit orders), but the cadence is still the Phase 2
    //   // spec: 09:30-16:00 ET, every 30 min. Add -open and -close companion
    //   // entries if Phase 2 spec requires opening-bell and closing-bell ticks.
    //   cron_restart: '*/30 10-15 * * 1-5',  // every 30 min, 10:00/10:30/.../15:30 ET
    // },
    // {
    //   name: 'invest-observer-loop',
    //   script: 'scripts/invest-observer-loop.sh',
    //   autorestart: false,
    //   cron_restart: '0 3 * * *',    // 03:00 HKT daily
    // },
  ],
};
