---
name: image
description: Image analysis -- describe and extract text from screenshots and images.
version: "1.0"
author: mini_agent
category: software-development
tools:
  - read_image
---

# Image Skill

Analyze images and screenshots. Use for:

- **read_image** -- describe an image file; supports screenshots, diagrams, photos, UI mockups

## When to Use
- User shares a screenshot of an error message
- User wants you to understand a diagram or UI mockup
- User provides a photo and asks questions about it

## Best Practices
- Call `read_image` with the file path; the model receives a text description
- Combine with `browser_screenshot` (web skill) for web UI analysis
- Image analysis is read-only; no modification of image files
