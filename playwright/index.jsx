import React from 'react';
import { beforeMount } from '@playwright/experimental-ct-react/hooks';

beforeMount(async () => {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => true,
  });
});
