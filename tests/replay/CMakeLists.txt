set(testrunner ${CMAKE_CURRENT_SOURCE_DIR}/run_test.sh)

add_test(NAME replay-one-fixture
         COMMAND ${testrunner} $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)

add_test(NAME replay-from-snapshot
         COMMAND python3 ${CMAKE_CURRENT_SOURCE_DIR}/run_resume.py
                 $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)
set_tests_properties(replay-from-snapshot PROPERTIES TIMEOUT 120)
