plugins {
    id("com.android.application") version "8.9.3" apply false
    id("com.chaquo.python") version "17.2.0" apply false
}

require(gradle.gradleVersion == "8.11.1") {
    "Use Gradle 8.11.1 for the pinned AGP 8.9.3 compatibility app; " +
        "the upstream demo wrapper is a different toolchain."
}
