#!/bin/bash
# Generate retro pixel art menu bar icons for Cloude Code

set -e

# Check if ImageMagick is installed
if ! command -v convert &> /dev/null; then
    echo "⚠️  ImageMagick not found. Please install it:"
    echo "   brew install imagemagick"
    echo ""
    echo "Or create icons manually (see README.md)"
    exit 1
fi

echo "🎨 Generating retro pixel art menu bar icons..."

# Color scheme
COLOR="#d77757"  # Orange/brown retro color

# Create 22x22 standard icon - pixel art character with arms
convert -size 22x22 xc:transparent \
    -fill "$COLOR" \
    -draw "rectangle 7,4 14,6" \
    -draw "rectangle 8,7 9,9" \
    -draw "rectangle 12,7 13,9" \
    -draw "rectangle 4,10 6,12" \
    -draw "rectangle 7,10 14,12" \
    -draw "rectangle 15,10 17,12" \
    -draw "rectangle 8,13 9,15" \
    -draw "rectangle 12,13 13,15" \
    iconTemplate.png

echo "✅ Created iconTemplate.png (22x22)"

# Create 44x44 retina icon - scaled up pixel art
convert -size 44x44 xc:transparent \
    -fill "$COLOR" \
    -draw "rectangle 14,8 28,12" \
    -draw "rectangle 16,14 18,18" \
    -draw "rectangle 24,14 26,18" \
    -draw "rectangle 8,20 12,24" \
    -draw "rectangle 14,20 28,24" \
    -draw "rectangle 30,20 34,24" \
    -draw "rectangle 16,26 18,30" \
    -draw "rectangle 24,26 26,30" \
    iconTemplate@2x.png

echo "✅ Created iconTemplate@2x.png (44x44)"

# Note about .icns
echo ""
echo "📝 Note: You'll need to create icon.icns for the app bundle."
echo "   Use the instructions in README.md or a tool like Icon Slate."
echo ""
echo "✨ Retro pixel art icons generated successfully!"
