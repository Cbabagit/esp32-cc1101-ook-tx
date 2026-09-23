// AGP 9 起 Kotlin 支持已内置, 不再需要 org.jetbrains.kotlin.android 插件。
// 显式加上反而会报 "no longer required for Kotlin support since AGP 9.0"。
plugins {
    id("com.android.application") version "9.4.1" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.4.20" apply false
}
