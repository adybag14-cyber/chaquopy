pluginManagement {
    val bootstrapRoot = providers.gradleProperty("bootstrap").orNull?.let { file(it).canonicalFile }
        ?: throw GradleException("Pass -Pbootstrap=/absolute/path/to/verified/fork/bootstrap")
    require(bootstrapRoot.resolve("receipt.json").isFile && bootstrapRoot.resolve("maven").isDirectory) {
        "The source-built fork bootstrap and its receipt are required before configuration."
    }
    repositories {
        exclusiveContent {
            forRepository {
                maven { url = uri(bootstrapRoot.resolve("maven")) }
            }
            filter { includeModule("com.chaquo.python.runtime", "bootstrap") }
        }
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "HermesChaquopyCompatibility"
include(":app")
