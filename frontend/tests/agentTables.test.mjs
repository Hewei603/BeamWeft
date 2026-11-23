import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import ReactMarkdown from 'react-markdown';
import gfm from 'remark-gfm';
import {preserveTableRows} from '../src/components/preserveTableRows.js';

const render = (text, plugins=[gfm,preserveTableRows]) =>
  renderToStaticMarkup(React.createElement(ReactMarkdown, {remarkPlugins:plugins},text));

test('scientific notation and compact operators remain in their columns and retain the final finding', () => {
  const result=render('| 轴 | 判据 | 状态 |\n|---|---|---|\n| T | |m| > 1 | **失稳** |\n| 标记 | `a|b` | 成功 |');
  assert.match(result, /<td>T<\/td>\s*<td>\|m\| &gt; 1<\/td>\s*<td><strong>失稳<\/strong><\/td>/);
  assert.match(result, /<td><code>a\|b<\/code><\/td>\s*<td>成功<\/td>/);
  const mixed=render('| Name | Expression | Outcome |\n|---|---|---|\n| run| `a | b` | ready |');
  assert.match(mixed, /<td>run<\/td>\s*<td><code>a \| b<\/code><\/td>\s*<td>ready<\/td>/);
});

test('ambiguous rows retain the entire original without adding unlabeled data columns', () => {
  const result=render('| Key | Value |\n|---|---|\n| A | B | C & D |\n| Other | Available |');
  assert.match(result, /colSpan="2".*?原文（列边界待核对）.*?\| A \| B \| C &amp; D \|<\/td>/i);
  assert.match(result, /<td>Other<\/td>\s*<td>Available<\/td>/);
});

test('valid GFM, escaped pipes, nested tables and fenced table examples retain their native rendering', () => {
  const text='> | A | B |\n> |---|---|\n> | X \\| Y | `a\\|b` |\n> | U | |\n\n```text\n| a | b |\n|---|---|\n| short | |m| + 3 | final |\n```';
  assert.equal(render(text), render(text,[gfm]));
  const nested=render('> | A | B |\n> |---|---|\n> | A|B | *Value* |');
  assert.match(nested, /<td>A\|B<\/td>\s*<td><em>Value<\/em><\/td>/);
});
