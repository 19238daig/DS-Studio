import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';
Object.defineProperty(window, 'matchMedia', {
  value: vi.fn().mockImplementation((query) => ({
    matches: false,
    media: query,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
