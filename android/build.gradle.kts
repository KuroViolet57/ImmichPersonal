// Image Panel: Android front-end for the ImmichPersonal panel (immich_organizer/web).
buildscript {
    dependencies {
        // AGP 9 compiles Kotlin itself ("built-in Kotlin"); this picks the Kotlin version it uses.
        classpath("org.jetbrains.kotlin:kotlin-gradle-plugin:2.4.20")
    }
}
plugins {
    id("com.android.application") version "9.4.1" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.4.20" apply false
}
