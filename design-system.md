# Design System

## 1. Product Design Direction

The product is a restaurant AI ordering system.

The visual language should communicate:
- Fast
- Reliable
- Modern
- Technical
- Restaurant-oriented
- Operational rather than gimmicky

The interface should feel like a serious product used by restaurant staff, not an AI toy.

## 2. Customer UI

The customer experience should prioritize the voice interaction.

Primary elements:
- Restaurant branding
- Microphone/voice control
- Conversation state
- Current cart
- Order total
- Order confirmation
- Order status

The user should never need to understand the underlying AI/tool architecture.

## 3. Voice Interaction UI

The microphone state must be obvious.

Recommended states:

### Idle
Customer can start speaking.

### Listening
Clearly indicate that speech is being captured.

### Thinking/processing
Show lightweight feedback without making the interface feel frozen.

### Speaking
Show that the AI is responding.

### Error
Explain the issue briefly and provide a recovery action.

Avoid excessive animations.

## 4. Cart UI

The cart should always show the current authoritative state.

Example:

```text
Zinger Burger     x2
Regular Fries     x1
Pepsi             x1
----------------------
Total             Rs. ...
```

When quantity changes, update visually immediately after backend confirmation.

Do not show stale optimistic state as authoritative.

## 5. KDS UI

The KDS should be operationally dense but readable.

Each order card should show:
- Order ID
- Time
- Items
- Quantities
- Total
- Current status
- Action to move to next status

Statuses:

`confirmed`
`preparing`
`ready`
`completed`

New orders should be visually distinguishable without relying only on color.

## 6. Visual Principles

- Clear hierarchy
- High readability
- Strong contrast
- Minimal unnecessary decoration
- Consistent spacing
- Responsive layout
- Keyboard accessibility where relevant
- Clear error states
- Avoid dashboard clutter

## 7. Restaurant Brand Adaptability

The system should eventually support restaurant-specific:
- Logo
- Colors
- Menu
- Branches
- Contact information
- Voice/personality settings

Do not hard-code the entire UI around one restaurant's brand.

## 8. Language

UI text should support English initially.

Voice conversation supports:
- English
- Roman Urdu
- Mixed speech

Avoid hard-coding language-specific business logic into UI components.

## 9. Design for Demo Reliability

For a hackathon/demo:
- Make the primary happy path obvious.
- Make connection state visible.
- Make order creation visually obvious.
- Make KDS updates immediate.
- Make failures understandable.

Do not spend disproportionate time on decorative effects before the voice/order flow is reliable.


## Engineering Boundary

The design system defines UI behavior and visual consistency. It must not contain API keys, provider secrets, or provider-specific authentication details.

Voice/provider integration belongs to the application architecture and agent configuration, not the visual design system.
