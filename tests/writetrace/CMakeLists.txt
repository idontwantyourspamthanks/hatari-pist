set(testrunner ${CMAKE_CURRENT_SOURCE_DIR}/run_test.py)

add_test(NAME writetrace
         COMMAND python3 ${testrunner} $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)
set_tests_properties(writetrace PROPERTIES TIMEOUT 180)
