*** Settings ***

Resource        cumulusci/robotframework/Salesforce.robot

*** Test Cases ***

Via API
    ${first_name} =       Generate Random String
    ${last_name} =        Generate Random String
    ${contact_id} =       Salesforce Insert  Contact
    ...                     FirstName=${first_name}
    ...                     LastName=${last_name}
    &{contact} =          Salesforce Get  Contact  ${contact_id}
    Validate Contact      ${contact_id}  ${first_name}  ${last_name}


*** Keywords ***

Validate Contact
    [Arguments]          ${contact_id}  ${first_name}  ${last_name}
    # Validate via API
    &{contact} =     Salesforce Get  Contact  ${contact_id}
    Should Be Equal  ${first_name}  &{contact}[FirstName]
    Should Be Equal  ${last_name}  &{contact}[LastName]


