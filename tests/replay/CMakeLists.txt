set(testrunner ${CMAKE_CURRENT_SOURCE_DIR}/run_test.sh)

add_test(NAME replay-one-fixture
         COMMAND ${testrunner} $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)
