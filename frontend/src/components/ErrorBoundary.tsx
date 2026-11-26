import { Component, type ReactNode } from "react";

/** Keeps one part's crash from blanking the whole page, and offers a reload (an open tab can
 * outlive an upgrade, and then the old file for Radio and Settings is gone). */
export class ErrorBoundary extends Component<{ what: string; children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error(`${this.props.what} failed:`, error);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="crashed" role="alert">
        <p>The {this.props.what} hit an error: {this.state.error.message}</p>
        <button type="button" className="rd-link" onClick={() => location.reload()}>Reload the page</button>
      </div>
    );
  }
}
