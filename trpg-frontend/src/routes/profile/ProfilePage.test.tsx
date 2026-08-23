import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

/**
 * 改密码那一屏（`exec/46` B6）。
 *
 * 守的是**两条只有前端能守的**：
 *
 * - **两次输入不一致要在这里拦**——后端只收一个新密码，它没法知道你打错了。
 * - **成功之后不许跳登录页**——后端只踢掉*其它*会话，当前这条是活的。
 *   把做对事的人踢回登录页，是拿一次安全动作去惩罚他。
 */

const changePassword = vi.fn()
const fetchMe = vi.fn()

vi.mock('@/services/auth', async () => {
  const actual = await vi.importActual<typeof import('@/services/auth')>('@/services/auth')
  return {
    ...actual,
    fetchMe: () => fetchMe(),
    changePassword: (a: string, b: string) => changePassword(a, b),
    updateProfile: vi.fn(),
    logout: vi.fn(),
  }
})

const { default: ProfilePage } = await import('./ProfilePage')

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  changePassword.mockReset()
  changePassword.mockResolvedValue(undefined)
  fetchMe.mockResolvedValue({ userId: 'u1', account: 'alice', nickname: '爱丽丝' })
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

function buttonSaying(text: string): HTMLButtonElement {
  const found = [...container.querySelectorAll('button')].find((b) =>
    (b.textContent ?? '').includes(text)
  )
  if (!found) throw new Error(`没找到写着「${text}」的按钮`)
  return found as HTMLButtonElement
}

function fieldWithPlaceholder(text: string): HTMLInputElement {
  const found = [...container.querySelectorAll('input')].find((i) =>
    (i.placeholder ?? '').includes(text)
  )
  if (!found) throw new Error(`没找到 placeholder 含「${text}」的输入框`)
  return found as HTMLInputElement
}

/** React 受控输入：直接设 .value 不会触发 onChange（判据全集里记过）。 */
function type(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
  setter?.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

async function mountAndOpen() {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/home/profile']}>
        <ProfilePage />
      </MemoryRouter>
    )
  })
  await flush()
  await act(async () => buttonSaying('改密码').click())
}

async function fill(oldPw: string, a: string, b: string) {
  await act(async () => {
    type(fieldWithPlaceholder('原密码'), oldPw)
    type(fieldWithPlaceholder('新密码'), a)
    type(fieldWithPlaceholder('再输一遍'), b)
  })
}

it('两次新密码不一致时不发请求', async () => {
  await mountAndOpen()
  await fill('secret1', 'secret2', 'secret3')
  await act(async () => buttonSaying('确认修改').click())
  await flush()

  expect(changePassword).not.toHaveBeenCalled()
  expect(container.textContent).toContain('两次输入的新密码不一样')
})

it('一致时把两个密码原样交给后端', async () => {
  await mountAndOpen()
  await fill('secret1', 'secret2', 'secret2')
  await act(async () => buttonSaying('确认修改').click())
  await flush()

  expect(changePassword).toHaveBeenCalledWith('secret1', 'secret2')
  expect(container.textContent).toContain('密码已改')
})

it('成功之后留在本页——当前这条会话是活的', async () => {
  await mountAndOpen()
  await fill('secret1', 'secret2', 'secret2')
  await act(async () => buttonSaying('确认修改').click())
  await flush()

  // 还在个人信息页：昵称、账号那两块仍然在
  expect(container.textContent).toContain('账号')
  expect(container.textContent).toContain('退出登录')
})

it('后端那句话原样显示——「原密码不正确」和「不能和原密码一样」是不同的下一步', async () => {
  changePassword.mockRejectedValue(new Error('原密码不正确'))
  await mountAndOpen()
  await fill('wrong', 'secret2', 'secret2')
  await act(async () => buttonSaying('确认修改').click())
  await flush()

  expect(container.textContent).toContain('原密码不正确')
})

it('代价写在按下之前：别的设备会被踢下线', async () => {
  await mountAndOpen()
  expect(container.textContent).toContain('别的设备上的登录会失效')
})
