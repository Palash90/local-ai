import React from 'react';
import { test, expect } from './fixtures.js';
import StatusBox from '../src/components/StatusBox';

test('StatusBox renders thinking by default', async ({ mount }) => {
  const component = await mount(<StatusBox message="Thinking about the universe..." />);
  await expect(component).toHaveAttribute('data-state', 'thinking');
  await expect(component).toContainText('Thinking about the universe...');
});

test('StatusBox detects search state', async ({ mount }) => {
  const component = await mount(<StatusBox message="Searching web for cats..." />);
  await expect(component).toHaveAttribute('data-state', 'search');
  await expect(component).toContainText('\uD83D\uDD0D');
});

test('StatusBox detects generate-image state', async ({ mount }) => {
  const component = await mount(<StatusBox message="Generating image..." />);
  await expect(component).toHaveAttribute('data-state', 'generate-image');
});

test('StatusBox detects edit-image state', async ({ mount }) => {
  const component = await mount(<StatusBox message="Editing image..." />);
  await expect(component).toHaveAttribute('data-state', 'edit-image');
});

test('StatusBox falls back to thinking for unknown messages', async ({ mount }) => {
  const component = await mount(<StatusBox message="Weird message" />);
  await expect(component).toHaveAttribute('data-state', 'thinking');
  await expect(component).toContainText('Weird message');
});

test('StatusBox shows default text when message is empty', async ({ mount }) => {
  const component = await mount(<StatusBox />);
  await expect(component).toHaveAttribute('data-state', 'thinking');
  await expect(component).toContainText('Thinking...');
});
