package com.lightstick.player.ui.theme

import android.app.Activity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

private val LightColors = lightColorScheme(
    primary = BrandDeep,
    onPrimary = Color.White,
    primaryContainer = BrandLight,
    onPrimaryContainer = BrandDarker,
    secondary = BrandDeep,
    onSecondary = Color.White,
    secondaryContainer = BrandLight,
    onSecondaryContainer = BrandDarker,
    tertiary = BrandDarker,
    onTertiary = Color.White,
    background = Color(0xFFF6FAFD),
    onBackground = Color(0xFF101418),
    surface = Color.White,
    onSurface = Color(0xFF101418),
    surfaceVariant = Color(0xFFE3EDF4),
    onSurfaceVariant = Color(0xFF41525E),
    outline = Color(0xFF93A6B3),
    error = Color(0xFFBA1A1A),
    onError = Color.White,
)

private val DarkColors = darkColorScheme(
    primary = Brand,
    onPrimary = Color(0xFF00344A),
    primaryContainer = BrandDarker,
    onPrimaryContainer = BrandLight,
    secondary = Brand,
    onSecondary = Color(0xFF00344A),
    secondaryContainer = Color(0xFF14405C),
    onSecondaryContainer = BrandLight,
    tertiary = Brand,
    onTertiary = Color(0xFF00344A),
    background = Color(0xFF0D1418),
    onBackground = Color(0xFFE0E6EA),
    surface = BrandSurface,
    onSurface = Color(0xFFE0E6EA),
    surfaceVariant = Color(0xFF1D2C36),
    onSurfaceVariant = Color(0xFFB7C6D1),
    outline = Color(0xFF6B7D89),
    error = Color(0xFFFFB4AB),
    onError = Color(0xFF690005),
)

@Composable
fun LightstickTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val scheme = if (darkTheme) DarkColors else LightColors
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars = !darkTheme
        }
    }
    MaterialTheme(colorScheme = scheme, content = content)
}
