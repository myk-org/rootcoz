const CHECK_INTERVAL = 10_000; // 10 seconds
const MAX_FAILURES = 3; // 30 seconds of failures before shutdown

export function startWatchdog(healthUrl: string, onDead: () => void): void {
  let consecutiveFailures = 0;

  setInterval(async () => {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      const resp = await fetch(healthUrl, { signal: controller.signal });
      clearTimeout(timeout);
      if (resp.ok) {
        consecutiveFailures = 0;
      } else {
        consecutiveFailures++;
      }
    } catch {
      consecutiveFailures++;
    }

    if (consecutiveFailures >= MAX_FAILURES) {
      onDead();
    }
  }, CHECK_INTERVAL);
}
