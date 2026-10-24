import { Component, type ReactNode } from 'react';

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/** 根部错误边界：组件崩溃时给出可恢复的提示页，而不是白屏。 */
export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="app-error-page">
          <div className="app-error-page__card">
            <h1>页面出了点问题</h1>
            <p>请刷新重试；若持续出现，请联系管理员并附以下信息。</p>
            <pre>{this.state.error.message}</pre>
            <button type="button" className="btn-primary" onClick={() => window.location.reload()}>刷新页面</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
