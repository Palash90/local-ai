import React from 'react';
import { test, expect } from '@playwright/experimental-ct-react';
import ImageLightbox from '../src/components/ImageLightbox';

test('inspect ImageLightbox DOM', async ({ page, mount }) => {
  const component = await mount(<ImageLightbox src="/output/gen.png" onClose={() => {}} />);
  await page.waitForTimeout(500);
  const info = await page.evaluate(() => {
    return {
      html: document.body.innerHTML,
      hasOverlay: !!document.querySelector('#image-overlay'),
      overlayCount: document.querySelectorAll('#image-overlay').length,
      rootChildren: Array.from(document.getElementById('root').children).map((c) => c.outerHTML.slice(0, 200)),
    };
  });
  console.log(JSON.stringify(info, null, 2));
});
