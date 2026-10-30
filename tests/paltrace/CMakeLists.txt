set(testrunner ${CMAKE_CURRENT_SOURCE_DIR}/run_test.py)

add_test(NAME paltrace
         COMMAND python3 ${testrunner} $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)
set_tests_properties(paltrace PROPERTIES TIMEOUT 180)
