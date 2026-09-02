import java.security.MessageDigest

plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

val selectedPython = providers.gradleProperty("pythonVersion").getOrElse("3.14")
require(selectedPython in listOf("3.13", "3.14")) {
    "pythonVersion must be 3.13 or 3.14, not '$selectedPython'"
}
val wheelhouseSetting = providers.gradleProperty("wheelhouse").orNull
    ?: throw GradleException("Pass -Pwheelhouse=/absolute/path/to/verified/wheels")
val wheelhouse = rootProject.file(wheelhouseSetting).canonicalFile
require(wheelhouse.isDirectory && wheelhouse.listFiles().orEmpty().any { it.extension == "whl" }) {
    "wheelhouse must be an existing directory containing verified wheels: $wheelhouse"
}

val generatedRoot = rootProject.file(".generated")
val generatedPython = generatedRoot.resolve("python")
val generatedSmoke = generatedPython.resolve("runtime_smoke.py")
val canonicalSmoke = rootProject.file("../runtime_smoke.py")
val smokeDigest = generatedRoot.resolve("runtime_smoke.sha256")
val generatedVersion = generatedRoot.resolve("python-version.txt")
val generatedWheelhouse = generatedRoot.resolve("wheelhouse.txt")
fun digest(file: File): String = MessageDigest.getInstance("SHA-256")
    .digest(file.readBytes()).joinToString("") { "%02x".format(it.toInt() and 0xff) }

require(generatedSmoke.isFile && smokeDigest.isFile && generatedVersion.isFile && generatedWheelhouse.isFile) {
    "Run python compat/hermes/prepare_app.py --python-version $selectedPython " +
        "--wheelhouse \"$wheelhouse\" before building."
}
require(generatedVersion.readText().trim() == selectedPython) {
    "Prepared Python version differs from pythonVersion; rerun prepare_app.py."
}
require(file(generatedWheelhouse.readText().trim()).canonicalFile == wheelhouse) {
    "Prepared wheelhouse differs from -Pwheelhouse; rerun prepare_app.py."
}
require(digest(generatedSmoke) == smokeDigest.readText().trim() && digest(canonicalSmoke) == digest(generatedSmoke)) {
    "Generated runtime_smoke.py is stale or modified; rerun prepare_app.py."
}

android {
    namespace = "com.mobilefork.chaquopycompat"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.mobilefork.chaquopycompat"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "0.1-python$selectedPython"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

chaquopy {
    defaultConfig {
        version = selectedPython
        providers.gradleProperty("buildPython").orNull?.let { buildPython(it) }
        pip {
            // Never build an unplanned native sdist or fall back to a Hermes
            // stub. jiter/core and pure msgpack come from our verified artifacts;
            // aiohttp and the remaining genuine PyPI distributions are already
            // hash-checked in the complete offline wheelhouse.
            options("--no-index", "--find-links", wheelhouse.absolutePath,
                "--only-binary", ":all:")
            install("-r", rootProject.file("requirements.txt").absolutePath)
        }
    }
    sourceSets.getByName("main") {
        srcDir(generatedPython)
    }
}

dependencies {
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
}
