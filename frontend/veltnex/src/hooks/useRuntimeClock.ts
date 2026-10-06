import * as React from "react";

/** Expire observations even if the API or polling stops responding. */
export function useRuntimeClock() {
  const [now, setNow] = React.useState(Date.now);
  React.useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}
