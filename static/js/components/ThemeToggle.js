export function initThemeToggle() {
  const body = document.body;
  const themeIcon = document.getElementById('theme-icon');
  
  const savedTheme = localStorage.getItem('theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  
  if (savedTheme === 'dark' || (!savedTheme && prefersDark)) {
    body.classList.add('dark-mode');
    if (themeIcon) themeIcon.textContent = '☀️';
  }

  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    if (body.classList.contains('dark-mode')) {
      body.classList.remove('dark-mode');
      if (themeIcon) themeIcon.textContent = '🌙';
      localStorage.setItem('theme', 'light');
    } else {
      body.classList.add('dark-mode');
      if (themeIcon) themeIcon.textContent = '☀️';
      localStorage.setItem('theme', 'dark');
    }
  });
}
