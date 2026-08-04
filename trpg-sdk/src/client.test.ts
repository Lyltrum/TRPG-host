/**
 * `ApiClient` 的 header 合并逻辑测试（issue #75）。
 *
 * 用 node:test（不引入 vitest 之类的 devDependency——SDK 已经零运行时依赖，
 * 测试跑起来也不需要更重的框架，node 自带的 test runner 加 tsx 转译足够）。
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ApiClient } from './client';

/** 造一个记录下"实际发给 fetch 的 headers"的假 fetch，用来断言合并结果。 */
function captureHeaders(): { client: ApiClient; captured: () => Headers | undefined } {
  let captured: Headers | undefined;
  const fakeFetch = (async (_input: string, init?: RequestInit) => {
    captured = new Headers(init?.headers);
    return new Response(JSON.stringify({ success: true, data: null, error: null }));
  }) as typeof fetch;

  const client = new ApiClient({ baseUrl: 'http://test', fetch: fakeFetch });
  return { client, captured: () => captured };
}

test('header 合并：Record<string,string> 形态', async () => {
  const { client, captured } = captureHeaders();
  await client.get('/x', { headers: { Authorization: 'Bearer abc' } });
  assert.equal(captured()?.get('authorization'), 'Bearer abc');
  assert.equal(captured()?.get('content-type'), 'application/json');
});

test('header 合并：Headers 实例形态', async () => {
  const { client, captured } = captureHeaders();
  await client.get('/x', { headers: new Headers({ Authorization: 'Bearer abc' }) });
  assert.equal(captured()?.get('authorization'), 'Bearer abc');
  assert.equal(captured()?.get('content-type'), 'application/json');
});

test('header 合并：string[][] 形态', async () => {
  const { client, captured } = captureHeaders();
  await client.get('/x', { headers: [['Authorization', 'Bearer abc']] });
  assert.equal(captured()?.get('authorization'), 'Bearer abc');
  assert.equal(captured()?.get('content-type'), 'application/json');
});

test('调用方传的 header 可以覆盖默认的 Content-Type', async () => {
  const { client, captured } = captureHeaders();
  await client.get('/x', { headers: { 'Content-Type': 'text/plain' } });
  assert.equal(captured()?.get('content-type'), 'text/plain');
});

/**
 * multipart 上传（模组导入，`exec/29` 第 5 步）——全项目第一个非 JSON 出口。
 *
 * 🔴 这一组守的是一个**会静默坏掉**的东西：`request` 原本无条件设
 * `Content-Type: application/json`，而 multipart 的那个头必须带
 * `boundary=...`，且 boundary 是运行时序列化 FormData 时才生成的。写死成
 * json（甚至写死成不带 boundary 的 multipart/form-data）都会让**后端**收到一个
 * 解析不了的 body——前端只看到"上传失败"，查起来很远。
 */
function captureRequest(): {
  client: ApiClient;
  captured: () => { headers?: Headers; init?: RequestInit };
} {
  const box: { headers?: Headers; init?: RequestInit } = {};
  const fakeFetch = (async (_input: string, init?: RequestInit) => {
    box.headers = new Headers(init?.headers);
    box.init = init;
    return new Response(JSON.stringify({ success: true, data: null, error: null }));
  }) as typeof fetch;

  return { client: new ApiClient({ baseUrl: 'http://test', fetch: fakeFetch }), captured: () => box };
}

test('postForm 不写 Content-Type，交给运行时生成 boundary', async () => {
  const { client, captured } = captureRequest();
  const form = new FormData();
  form.append('file', new Blob(['x']), 'm.txt');

  await client.postForm('/modules/import', form);

  assert.equal(captured().headers?.get('content-type'), null);
});

test('postForm 把 FormData 原样交给 fetch，不做 JSON 序列化', async () => {
  const { client, captured } = captureRequest();
  const form = new FormData();
  form.append('file', new Blob(['x']), 'm.txt');

  await client.postForm('/modules/import', form);

  assert.ok(captured().init?.body instanceof FormData);
  assert.equal(captured().init?.method, 'POST');
});

test('postForm 仍然合并调用方的 header（比如 Authorization）', async () => {
  const { client, captured } = captureRequest();

  await client.postForm('/modules/import', new FormData(), {
    headers: { Authorization: 'Bearer abc' }
  });

  assert.equal(captured().headers?.get('authorization'), 'Bearer abc');
  assert.equal(captured().headers?.get('content-type'), null);
});

test('普通 post 不受影响，仍然是 application/json', async () => {
  const { client, captured } = captureRequest();

  await client.post('/x', { a: 1 });

  assert.equal(captured().headers?.get('content-type'), 'application/json');
  assert.equal(captured().init?.body, JSON.stringify({ a: 1 }));
});
