import * as React from "react";

interface State {
  hasError: boolean;
}

interface Props {
  /** Rendered in place of the children if they throw. Defaults to null. */
  fallback?: React.ReactNode;
  children: React.ReactNode;
}

/**
 * Minimal class-based error boundary. React 18 still requires a class
 * for componentDidCatch; this is the smallest viable wrapper.
 *
 * Use this around optional, decorative subtrees (e.g. WebGL widgets) so
 * a render failure inside them doesn't unmount the rest of the page.
 */
export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: unknown) {
    // Log to the console; surfacing a UI toast would be noisy for the
    // typical use case (background decorations).
    // eslint-disable-next-line no-console
    console.error("ErrorBoundary caught:", error);
  }

  render() {
    if (this.state.hasError) return this.props.fallback ?? null;
    return this.props.children;
  }
}
