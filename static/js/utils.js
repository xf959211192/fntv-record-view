export function escapeHtml(unsafe) {
  return String(unsafe)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

export function debounce(func, wait) {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
}

export function formatDateString(dateStr) {
  if (!dateStr) return '';
  const dt = new Date(dateStr);
  return dt.getFullYear() + '-' +
    String(dt.getMonth() + 1).padStart(2, '0') + '-' +
    String(dt.getDate()).padStart(2, '0') + ' ' +
    String(dt.getHours()).padStart(2, '0') + ':' +
    String(dt.getMinutes()).padStart(2, '0') + ':' +
    String(dt.getSeconds()).padStart(2, '0');
}

export function createComponent(initFunc) {
  // simple wrapper to signify component logic
  return {
    mount(containerId) {
      const el = document.getElementById(containerId);
      if (el) {
        initFunc(el);
      } else {
        console.warn(`Container ${containerId} not found`);
      }
    }
  }
}
