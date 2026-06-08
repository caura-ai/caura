import { test, expect, type Page } from '@playwright/test'

const BASE = 'http://localhost'

// Expected nav links for SITE pages (public-nav component)
// Site has trailingSlash: true so all paths get trailing /
const SITE_NAV_LINKS = [
  { label: 'Features', href: '/#features' },
  { label: 'How It Works', href: '/#how-it-works' },
  { label: 'Architecture', href: '/architecture/' },
  { label: 'Demo', href: '/#demo' },
  { label: 'Pricing', href: '/pricing/' },
  { label: 'API Docs', href: '/docs/' },
  { label: 'Setup Guide', href: '/guide/' },
  { label: 'For Agents', href: '/for-agents/' },
]

// Expected nav links for the LANDING page (inline header)
const LANDING_NAV_LINKS = [
  { label: 'Features', href: '#features' },
  { label: 'How It Works', href: '#how-it-works' },
  { label: 'Architecture', href: '/architecture/' },
  { label: 'Demo', href: '#demo' },
  { label: 'Pricing', href: '/pricing/' },
  { label: 'API Docs', href: '/docs/' },
  { label: 'Setup Guide', href: '/guide/' },
  { label: 'For Agents', href: '/for-agents/' },
]

// All site sub-pages that should have PublicNav
const SITE_PAGES = [
  { path: '/pricing', title: 'Pricing' },
  { path: '/docs', title: 'API Docs' },
  { path: '/architecture', title: 'Architecture' },
  { path: '/guide', title: 'Setup Guide' },
  { path: '/for-agents', title: 'For Agents' },
  { path: '/terms', title: 'Terms' },
]

async function getNavLinks(page: Page) {
  const nav = page.locator('nav').first()
  const links = nav.locator('a')
  const count = await links.count()
  const result: { label: string; href: string }[] = []
  for (let i = 0; i < count; i++) {
    const el = links.nth(i)
    const text = (await el.textContent())?.trim() || ''
    const href = await el.getAttribute('href') || ''
    // Skip logo link (href is exactly "/" with no text)
    if (text && href !== '/') {
      result.push({ label: text, href })
    }
  }
  return result
}

async function getHeaderLinks(page: Page) {
  const header = page.locator('header').first()
  const links = header.locator('nav a')
  const count = await links.count()
  const result: { label: string; href: string }[] = []
  for (let i = 0; i < count; i++) {
    const el = links.nth(i)
    const text = (await el.textContent())?.trim() || ''
    const href = await el.getAttribute('href') || ''
    if (text) result.push({ label: text, href })
  }
  return result
}

test.describe('Landing page navbar', () => {
  test('has correct nav links and Sign In button', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' })

    // Check the inline header nav
    const headerLinks = await getHeaderLinks(page)
    console.log('Landing page header links:', JSON.stringify(headerLinks, null, 2))

    for (const expected of LANDING_NAV_LINKS) {
      const found = headerLinks.find(l => l.label === expected.label)
      expect(found, `Missing nav link: ${expected.label}`).toBeTruthy()
      expect(found!.href).toBe(expected.href)
    }

    // Sign In button
    const signIn = page.locator('header a[href*="signin"]')
    await expect(signIn).toBeVisible()

    // Page should have CSS loaded (not unstyled)
    const header = page.locator('header').first()
    const bg = await header.evaluate(el => getComputedStyle(el).backgroundColor)
    expect(bg, 'Header should have background styling').not.toBe('')
    expect(bg).not.toBe('rgba(0, 0, 0, 0)') // not transparent/unstyled
  })
})

test.describe('Site sub-pages navbar', () => {
  for (const sp of SITE_PAGES) {
    test(`${sp.path} has PublicNav with correct links`, async ({ page }) => {
      await page.goto(`${BASE}${sp.path}`, { waitUntil: 'networkidle' })

      // Must have a <nav> element
      const nav = page.locator('nav').first()
      await expect(nav, `${sp.path} should have a <nav>`).toBeVisible()

      // Check all expected links
      const links = await getNavLinks(page)
      console.log(`${sp.path} nav links:`, JSON.stringify(links, null, 2))

      for (const expected of SITE_NAV_LINKS) {
        const found = links.find(l => l.label === expected.label)
        expect(found, `${sp.path} missing nav link: ${expected.label}`).toBeTruthy()
        expect(found!.href, `${sp.path} wrong href for ${expected.label}`).toBe(expected.href)
      }

      // Sign In button in nav
      const signIn = nav.locator('a[href*="signin"]')
      await expect(signIn, `${sp.path} should have Sign In`).toBeVisible()

      // CSS is loaded — nav should have background color
      const navBg = await nav.evaluate(el => getComputedStyle(el).backgroundColor)
      expect(navBg, `${sp.path} nav should be styled`).not.toBe('rgba(0, 0, 0, 0)')
    })
  }
})

test.describe('Footer on site sub-pages', () => {
  for (const sp of SITE_PAGES) {
    test(`${sp.path} has PublicFooter`, async ({ page }) => {
      await page.goto(`${BASE}${sp.path}`, { waitUntil: 'networkidle' })

      const footer = page.locator('footer').first()
      await expect(footer, `${sp.path} should have a <footer>`).toBeVisible()

    })
  }
})

test.describe('Nav link consistency across site pages', () => {
  test('all site sub-pages have identical nav links', async ({ page }) => {
    const allPageLinks: Record<string, { label: string; href: string }[]> = {}

    for (const sp of SITE_PAGES) {
      await page.goto(`${BASE}${sp.path}`, { waitUntil: 'networkidle' })
      allPageLinks[sp.path] = await getNavLinks(page)
    }

    // Compare each page's links to the first page
    const reference = allPageLinks[SITE_PAGES[0].path]
    const refLabels = reference.map(l => l.label).sort().join(',')

    for (const sp of SITE_PAGES.slice(1)) {
      const labels = allPageLinks[sp.path].map(l => l.label).sort().join(',')
      expect(labels, `${sp.path} nav links differ from ${SITE_PAGES[0].path}`).toBe(refLabels)
    }
  })
})

// Expected nav links for MAIN APP public pages (not logged in)
// Main app doesn't have trailingSlash so hrefs have no trailing /
const MAIN_APP_NAV_LINKS = [
  { label: 'Features', href: '/#features' },
  { label: 'How It Works', href: '/#how-it-works' },
  { label: 'Architecture', href: '/architecture/' },
  { label: 'Demo', href: '/#demo' },
  { label: 'Pricing', href: '/pricing/' },
  { label: 'API Docs', href: '/docs/' },
  { label: 'Setup Guide', href: '/guide/' },
  { label: 'For Agents', href: '/for-agents/' },
]

const MAIN_APP_PUBLIC_PAGES = [
  { path: '/signin', title: 'Sign In' },
  { path: '/forgot-password', title: 'Forgot Password' },
]

test.describe('Main app public pages navbar', () => {
  for (const mp of MAIN_APP_PUBLIC_PAGES) {
    test(`${mp.path} has PublicNav with correct links`, async ({ page }) => {
      await page.goto(`${BASE}${mp.path}`, { waitUntil: 'networkidle' })

      const nav = page.locator('nav').first()
      await expect(nav, `${mp.path} should have a <nav>`).toBeVisible()

      const links = await getNavLinks(page)
      console.log(`${mp.path} nav links:`, JSON.stringify(links, null, 2))

      for (const expected of MAIN_APP_NAV_LINKS) {
        const found = links.find(l => l.label === expected.label)
        expect(found, `${mp.path} missing nav link: ${expected.label}`).toBeTruthy()
        expect(found!.href, `${mp.path} wrong href for ${expected.label}`).toBe(expected.href)
      }

      // Sign In button
      const signIn = nav.locator('a[href*="signin"]')
      await expect(signIn, `${mp.path} should have Sign In`).toBeVisible()
    })
  }

  test('signin form is not pushed down by double min-h-screen', async ({ page }) => {
    await page.goto(`${BASE}/signin`, { waitUntil: 'networkidle' })

    const card = page.locator('.max-w-md').first()
    const box = await card.boundingBox()
    // Card top should be within reasonable distance from nav (not pushed to middle of 2x viewport)
    expect(box!.y, 'Sign-in card should not be pushed far down').toBeLessThan(400)
  })
})
