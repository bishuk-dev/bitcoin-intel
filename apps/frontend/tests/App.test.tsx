import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from '../src/App'

describe('Phase 0 application', () => {
  it('renders the initialized foundation message', () => {
    render(<App />)

    expect(
      screen.getByRole('heading', { name: 'Bitcoin Intelligence Platform' }),
    ).toBeVisible()
    expect(screen.getByText('System foundation initialized.')).toBeVisible()
    expect(screen.getByText('Phase 0')).toBeVisible()
  })
})
