/** 主题：light / dark / 跟随系统（未设置时）。持久化在 localStorage。 */

const THEME_KEY = 'crosscomply.theme';

export type Theme = 'light' | 'dark';

function systemPrefersDark(): boolean {
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

export function currentTheme(): Theme {
  const stored = window.localStorage.getItem(THEME_KEY);
  if (stored === 'light' || stored === 'dark') return stored;
  return systemPrefersDark() ? 'dark' : 'light';
}

export function applyTheme(theme: Theme): void {
  // 显式写入 light/dark：「跟随系统」由解析后的具体值表达，
  // 否则删除属性后 `:root:not([data-theme='light'])` 在系统深色下仍会命中深色覆盖
  document.documentElement.dataset.theme = theme;
}

export function storedPreference(): 'light' | 'dark' | 'system' {
  const stored = window.localStorage.getItem(THEME_KEY);
  return stored === 'light' || stored === 'dark' ? stored : 'system';
}

export function setTheme(theme: Theme): void {
  window.localStorage.setItem(THEME_KEY, theme);
  applyTheme(theme);
}

/** 跟随系统模式下监听系统切换。 */
export function watchSystemTheme(): void {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (storedPreference() === 'system') applyTheme(currentTheme());
  });
}
